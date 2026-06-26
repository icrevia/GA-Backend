import logging
from typing import Dict
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
            
            engine = LudoEngine(match_id, colors)
            engine.state = "PLAYING"
            self.games[match_id] = engine

            await self.broadcast_state(match_id)

    async def handle_action(self, match_id: int, user_id: int, action: dict):
        if match_id not in self.games:
            return
            
        engine = self.games[match_id]
        
        # We need mapping from user_id to color
        # In a real app we would cache this mapping
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

        action_type = action.get("action")
        
        if action_type == "ROLL_DICE":
            engine.roll_dice(player_color)
            await self.broadcast_state(match_id)
            
        elif action_type == "MOVE_TOKEN":
            token_index = action.get("token_index")
            if token_index is not None:
                success = engine.move_token(player_color, token_index)
                if success:
                    await self.broadcast_state(match_id)
                    if engine.state == "COMPLETED":
                        await self.end_game(match_id, engine.winner)

    async def broadcast_state(self, match_id: int):
        if match_id not in self.games:
            return
        state = self.games[match_id].get_state()
        await manager.broadcast_to_ludo(match_id, {"type": "LUDO_STATE", "payload": state})

    async def end_game(self, match_id: int, winner_color: str):
        if match_id not in self.games:
            return
            
        # Distribute prize and update DB
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
                    # Add wallet transaction logic here
                    
                await db.commit()
                
        del self.games[match_id]

orchestrator = LudoOrchestrator()
