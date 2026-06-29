"""
LudoOrchestrator — routes WebSocket actions to LudoEngine instances.

Performance targets:
  • handle_action():  < 1 ms   (pure in-memory, zero DB, zero I/O)
  • broadcast_state(): < 2 ms  (JSON serialize + WS send)
  • end_game():       async DB write — only called once per match

Key design decisions
─────────────────────
1. No DB hit in handle_action():
   Color lookup is resolved once at game start and stored in _color_cache.
   If a user somehow sends an action whose color isn't cached, we silently
   drop it (not a valid participant).

2. broadcast_state() sends the full state dict — the engine's get_state()
   already includes valid_moves, so the client never needs a second call.

3. Turn timer fires every 1 s. It mutates engine state in-process (no lock
   needed — Python's GIL protects single-slot mutations) and broadcasts.

4. Wallet payout happens entirely inside end_game() which runs in a background
   task so it never blocks the WS loop.
"""

import logging
import time as _time
import asyncio
from typing import Dict, Optional, Tuple

from core.websockets import manager
from services.ludo_engine import LudoEngine
from core.database import SessionLocal
from models.ludo import LudoMatch, LudoParticipant
from sqlalchemy.future import select

logger = logging.getLogger("GamerzAdda.LudoOrchestrator")


class LudoOrchestrator:
    __slots__ = ("games", "timers", "_color_cache")

    def __init__(self) -> None:
        # match_id → LudoEngine
        self.games: Dict[int, LudoEngine] = {}
        # match_id → asyncio.Task
        self.timers: Dict[int, asyncio.Task] = {}
        # match_id → {user_id: color_str}  — populated once at start_game, never hits DB again
        self._color_cache: Dict[int, Dict[int, str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_game(self, match_id: int) -> None:
        """
        Called by LudoMatchmaker after match row is committed.
        Does ONE DB read to load participant colors, then everything runs in-memory.
        """
        async with SessionLocal() as db:
            match_res = await db.execute(
                select(LudoMatch).where(LudoMatch.id == match_id)
            )
            match = match_res.scalar_one_or_none()
            if not match:
                logger.warning("start_game: match %d not found", match_id)
                return

            match.status = "PLAYING"
            await db.commit()

            p_res = await db.execute(
                select(LudoParticipant).where(LudoParticipant.match_id == match_id)
            )
            participants = p_res.scalars().all()

        colors = [p.color for p in participants]
        color_map = {p.user_id: p.color for p in participants}

        engine = LudoEngine(match_id, colors)
        engine.state = "PLAYING"
        engine.end_time_ms = int(_time.time() * 1000) + 7 * 60 * 1000  # 7 min

        self.games[match_id] = engine
        self._color_cache[match_id] = color_map

        # Broadcast initial state to both players
        await self._broadcast(match_id, engine)

        # Start turn timer
        self.timers[match_id] = asyncio.create_task(
            self._timer_loop(match_id), name=f"ludo_timer_{match_id}"
        )
        logger.info("Game started: match=%d players=%s", match_id, colors)

    # ------------------------------------------------------------------
    # Hot path — called on every player action
    # ------------------------------------------------------------------

    async def handle_action(self, match_id: int, user_id: int, action: dict) -> None:
        """
        Pure in-memory action handler.
        Target: < 1 ms excluding the final WS send.
        """
        engine = self.games.get(match_id)
        if engine is None:
            return

        # O(1) color lookup — no DB, no async
        color_map = self._color_cache.get(match_id)
        if color_map is None:
            return
        player_color = color_map.get(user_id)
        if player_color is None:
            logger.debug("handle_action: user %d not in match %d", user_id, match_id)
            return

        # Normalize action format — support both flat and nested payloads
        raw = action.get("action")
        if isinstance(raw, dict):
            action_type: Optional[str] = raw.get("action")
            action_data: dict = raw
        else:
            action_type = raw
            action_data = action

        # ---------- ROLL_DICE ----------
        if action_type == "ROLL_DICE":
            roll = engine.roll_dice(player_color)
            if roll == -1:
                return  # illegal — don't broadcast noise
            await self._broadcast(match_id, engine)
            return

        # ---------- MOVE_TOKEN ----------
        if action_type == "MOVE_TOKEN":
            token_index = action_data.get("token_index")
            if token_index is None:
                logger.debug("handle_action: MOVE_TOKEN missing token_index")
                return
            success = engine.move_token(player_color, int(token_index))
            if not success:
                return
            await self._broadcast(match_id, engine)
            if engine.state == "COMPLETED":
                # Schedule DB write + payout — don't block the WS loop
                asyncio.create_task(
                    self.end_game(match_id, engine.winner),
                    name=f"ludo_end_{match_id}",
                )
            return

        logger.warning(
            "handle_action: unknown action_type=%r from user=%d match=%d",
            action_type, user_id, match_id,
        )

    # ------------------------------------------------------------------
    # Broadcast — called after every state change
    # ------------------------------------------------------------------

    async def _broadcast(self, match_id: int, engine: LudoEngine) -> None:
        """Serialize state and push to all players in the ludo room."""
        state = engine.get_state()
        await manager.broadcast_to_ludo(match_id, {"type": "LUDO_STATE", "payload": state})

    # Keep legacy name for backward compat
    async def broadcast_state(self, match_id: int) -> None:
        engine = self.games.get(match_id)
        if engine:
            await self._broadcast(match_id, engine)

    # ------------------------------------------------------------------
    # Timer loop — background task, 1 s granularity
    # ------------------------------------------------------------------

    async def _timer_loop(self, match_id: int) -> None:
        while True:
            await asyncio.sleep(1)

            engine = self.games.get(match_id)
            if engine is None:
                break

            if engine.state == "COMPLETED":
                break

            now_ms = int(_time.time() * 1000)

            # 7-minute match timer
            if engine.end_time_ms > 0 and now_ms >= engine.end_time_ms:
                await self._force_end_by_timer(match_id)
                break

            # 10-second per-turn timer
            if engine.state == "PLAYING":
                turn_elapsed = now_ms - engine.turn_start_time_ms
                if turn_elapsed > 10_000:
                    current = engine.get_current_player()
                    logger.info(
                        "Turn timeout: match=%d player=%s", match_id, current
                    )
                    engine.next_turn()
                    await self._broadcast(match_id, engine)

    # ------------------------------------------------------------------
    # Force-end by timer
    # ------------------------------------------------------------------

    async def _force_end_by_timer(self, match_id: int) -> None:
        engine = self.games.get(match_id)
        if engine is None or engine.state == "COMPLETED":
            return

        engine.state = "COMPLETED"
        winner_color = self._determine_winner_by_score(engine)
        engine.winner = winner_color

        await self._broadcast(match_id, engine)
        asyncio.create_task(
            self.end_game(match_id, winner_color),
            name=f"ludo_end_{match_id}",
        )

    def _determine_winner_by_score(self, engine: LudoEngine) -> str:
        """Find highest scorer; tie-break by most tokens at finish (pos == 56)."""
        best_score = -1
        best: list = []
        for p, s in engine.scores.items():
            if s > best_score:
                best_score = s
                best = [p]
            elif s == best_score:
                best.append(p)

        if len(best) == 1:
            return best[0]

        # Tie-break: count tokens at TOTAL_CELLS (==56, not 57!)
        from services.ludo_engine import TOTAL_CELLS
        best_home = -1
        winner = best[0]
        for p in best:
            home_count = sum(1 for pos in engine.positions[p] if pos == TOTAL_CELLS)
            if home_count > best_home:
                best_home = home_count
                winner = p
        return winner

    # ------------------------------------------------------------------
    # End-game — runs as a background task (DB + wallet)
    # ------------------------------------------------------------------

    async def end_game(self, match_id: int, winner_color: Optional[str]) -> None:
        """
        Write match result to DB and credit winner's wallet.
        Runs AFTER broadcast so players see the result immediately.
        """
        if match_id not in self.games:
            # Already cleaned up (double-call guard)
            return

        # Snapshot the engine data we need before cleanup
        engine = self.games[match_id]
        prize_pool = None

        try:
            async with SessionLocal() as db:
                match_res = await db.execute(
                    select(LudoMatch).where(LudoMatch.id == match_id)
                )
                match = match_res.scalar_one_or_none()
                if not match:
                    logger.error("end_game: match %d not found in DB", match_id)
                    return

                match.status = "COMPLETED"
                prize_pool = match.prize_pool

                if winner_color:
                    w_res = await db.execute(
                        select(LudoParticipant).where(
                            LudoParticipant.match_id == match_id,
                            LudoParticipant.color == winner_color,
                        )
                    )
                    winner = w_res.scalar_one_or_none()
                    if winner:
                        winner.status = "WON"
                        match.winner_id = winner.user_id

                        # ---- Wallet payout ----
                        if prize_pool and prize_pool > 0:
                            await self._credit_winner(
                                db, winner.user_id, prize_pool, match_id
                            )

                # Mark all other participants as LOST
                losers_res = await db.execute(
                    select(LudoParticipant).where(
                        LudoParticipant.match_id == match_id,
                        LudoParticipant.status == "PLAYING",
                    )
                )
                for loser in losers_res.scalars().all():
                    loser.status = "LOST"

                await db.commit()
                logger.info(
                    "Match %d ended. Winner=%s Prize=₹%s",
                    match_id, winner_color, prize_pool,
                )

        except Exception:
            logger.exception("end_game: DB error for match %d", match_id)
        finally:
            # Always clean up in-memory state
            self._color_cache.pop(match_id, None)
            timer = self.timers.pop(match_id, None)
            if timer:
                timer.cancel()
            self.games.pop(match_id, None)

    async def _credit_winner(self, db, user_id: int, prize_pool, match_id: int) -> None:
        """Credit prize money to winner's wallet (winning bucket)."""
        try:
            from models.user import User
            from models.wallet import WalletTransaction
            from services.wallet_balances import credit_wallet, WALLET_BUCKET_WINNING, to_money
            import uuid

            user = await db.get(User, user_id)
            if not user:
                logger.error("_credit_winner: user %d not found", user_id)
                return

            amount = to_money(prize_pool)
            credit_wallet(user, amount, WALLET_BUCKET_WINNING)

            db.add(WalletTransaction(
                user_id=user_id,
                amount=amount,
                transaction_type="LUDO_WIN",
                status="SUCCESS",
                reference_id=f"LMM-WIN-{uuid.uuid4().hex[:8]}",
                remark=f"Ludo Match #{match_id} Prize",
            ))
            logger.info("Credited ₹%s to user %d for match %d", amount, user_id, match_id)
        except Exception:
            logger.exception("_credit_winner: failed for user=%d match=%d", user_id, match_id)


# Singleton — imported by ws.py and ludo_matchmaker.py
orchestrator = LudoOrchestrator()
