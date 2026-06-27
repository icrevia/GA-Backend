import logging
import time
import asyncio
from typing import Dict, Tuple
from core.websockets import manager
from services.ludo_engine import LudoEngine
from core.database import SessionLocal
from models.ludo import LudoMatch, LudoParticipant
from sqlalchemy.future import select

logger = logging.getLogger("GamerzAdda.LudoOrchestrator")

class LudoOrchestrator:
    def __init__(self):
        # match_id -> LudoEngine
        self.games: Dict[int, LudoEngine] = {}
        self.timers: Dict[int, asyncio.Task] = {}
        # Cache: match_id -> {user_id: color}  — avoids DB hit on every action
        self._color_cache: Dict[int, Dict[int, str]] = {}

    async def start_game(self, match_id: int):
        async with SessionLocal() as db:
            result = await db.execute(select(LudoMatch).where(LudoMatch.id == match_id))
            match = result.scalar_one_or_none()
            if not match:
                return

            match.status = "PLAYING"
            await db.commit()

            p_result = await db.execute(select(LudoParticipant).where(LudoParticipant.match_id == match_id))
            participants = p_result.scalars().all()
            colors = [p.color for p in participants]

            # Build color cache once at game start
            self._color_cache[match_id] = {p.user_id: p.color for p in participants}

            engine = LudoEngine(match_id, colors)
            engine.state = "PLAYING"

            # Set timer for 7 minutes
            now_ms = int(time.time() * 1000)
            engine.end_time_ms = now_ms + (7 * 60 * 1000)

            self.games[match_id] = engine

            await self.broadcast_state(match_id)

            # Start background timer
            self.timers[match_id] = asyncio.create_task(self._match_timer_task(match_id))

    async def _match_timer_task(self, match_id: int):
        await asyncio.sleep(7 * 60)
        await self.force_end_game_by_timer(match_id)

    async def force_end_game_by_timer(self, match_id: int):
        if match_id not in self.games:
            return

        engine = self.games[match_id]
        if engine.state == "COMPLETED":
            return

        engine.state = "COMPLETED"

        # Calculate winner by score
        highest_score = -1
        best_players = []
        for p, s in engine.scores.items():
            if s > highest_score:
                highest_score = s
                best_players = [p]
            elif s == highest_score:
                best_players.append(p)

        # Tie-breaker: tokens closest to home
        if len(best_players) > 1:
            most_home = -1
            true_winner = best_players[0]
            for p in best_players:
                home_count = sum(1 for pos in engine.positions[p] if pos == 57)
                if home_count > most_home:
                    most_home = home_count
                    true_winner = p
            engine.winner = true_winner
        else:
            engine.winner = best_players[0]

        await self.broadcast_state(match_id)
        await self.end_game(match_id, engine.winner)

    def _get_color(self, match_id: int, user_id: int) -> str | None:
        """Returns player color from in-memory cache. No DB hit."""
        cache = self._color_cache.get(match_id)
        if cache is None:
            return None
        return cache.get(user_id)

    def _parse_action(self, action: dict) -> Tuple[str | None, dict]:
        """Handles both flat {action:'ROLL_DICE'} and nested {action:{action:'ROLL_DICE'}} formats."""
        raw = action.get("action")
        if isinstance(raw, dict):
            return raw.get("action"), raw
        return raw, action

    async def handle_action(self, match_id: int, user_id: int, action: dict):
        if match_id not in self.games:
            return

        engine = self.games[match_id]

        # O(1) color lookup — no DB
        player_color = self._get_color(match_id, user_id)
        if player_color is None:
            # Fallback to DB only if not in cache (shouldn't normally happen)
            async with SessionLocal() as db:
                result = await db.execute(
                    select(LudoParticipant).where(
                        LudoParticipant.match_id == match_id,
                        LudoParticipant.user_id == user_id
                    )
                )
                participant = result.scalar_one_or_none()
                if not participant:
                    return
                player_color = participant.color
                # Populate cache
                if match_id not in self._color_cache:
                    self._color_cache[match_id] = {}
                self._color_cache[match_id][user_id] = player_color

        action_type, action_data = self._parse_action(action)

        if action_type == "ROLL_DICE":
            roll = engine.roll_dice(player_color)
            if roll != -1:
                await self.broadcast_state(match_id)
                # If no valid moves after roll, auto-pass after a short delay
                if not engine.has_valid_moves(player_color, roll):
                    asyncio.create_task(self._auto_pass_task(match_id, player_color))

        elif action_type == "MOVE_TOKEN":
            token_index = action_data.get("token_index")
            if token_index is None:
                token_index = action.get("token_index")
            if token_index is not None:
                success = engine.move_token(player_color, int(token_index))
                if success:
                    await self.broadcast_state(match_id)
                    if engine.state == "COMPLETED":
                        await self.end_game(match_id, engine.winner)
        else:
            logger.warning(f"LudoOrchestrator: Unknown action_type={action_type!r} from user={user_id}")

    async def broadcast_state(self, match_id: int):
        if match_id not in self.games:
            return
        state = self.games[match_id].get_state()
        await manager.broadcast_to_ludo(match_id, {"type": "LUDO_STATE", "payload": state})

    async def _auto_pass_task(self, match_id: int, player_color: str):
        """Called when a player rolls but has no valid moves. Passes their turn after a short delay."""
        await asyncio.sleep(1.2)
        if match_id not in self.games:
            return
        engine = self.games[match_id]
        if engine.state == "PLAYING" and engine.get_current_player() == player_color and engine.dice_rolled:
            engine.next_turn()
            await self.broadcast_state(match_id)

    async def end_game(self, match_id: int, winner_color: str):
        if match_id not in self.games:
            return

        async with SessionLocal() as db:
            result = await db.execute(select(LudoMatch).where(LudoMatch.id == match_id))
            match = result.scalar_one_or_none()
            if match:
                match.status = "COMPLETED"

                w_res = await db.execute(
                    select(LudoParticipant).where(
                        LudoParticipant.match_id == match_id,
                        LudoParticipant.color == winner_color
                    )
                )
                winner = w_res.scalar_one_or_none()

                if winner:
                    winner.status = "WON"
                    match.winner_id = winner.user_id
                    # TODO: Wallet transaction logic here

                await db.commit()

        # Cleanup
        self._color_cache.pop(match_id, None)
        self.timers.pop(match_id, None)
        del self.games[match_id]

orchestrator = LudoOrchestrator()
