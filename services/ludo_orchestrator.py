import logging
from typing import Dict
from core.websockets import manager
from services.ludo_engine import LudoEngine
from core.database import SessionLocal
from models.ludo import LudoMatch, LudoParticipant

logger = logging.getLogger("GamerzAdda.LudoOrchestrator")

class LudoOrchestrator:
    def __init__(self):
        # match_id -> LudoEngine
        self.games: Dict[int, LudoEngine] = {}

    async def start_game(self, match_id: int):
        with SessionLocal() as db:
            match = db.query(LudoMatch).filter(LudoMatch.id == match_id).first()
            if not match:
                return
            
            match.status = "PLAYING"
            db.commit()

            participants = db.query(LudoParticipant).filter(LudoParticipant.match_id == match_id).all()
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
        with SessionLocal() as db:
            participant = db.query(LudoParticipant).filter(
                LudoParticipant.match_id == match_id,
                LudoParticipant.user_id == user_id
            ).first()
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
        with SessionLocal() as db:
            match = db.query(LudoMatch).filter(LudoMatch.id == match_id).first()
            if match:
                match.status = "COMPLETED"
                
                winner = db.query(LudoParticipant).filter(
                    LudoParticipant.match_id == match_id,
                    LudoParticipant.color == winner_color
                ).first()
                
                if winner:
                    winner.status = "WON"
                    match.winner_id = winner.user_id
                    # Add wallet transaction logic here
                    
                db.commit()
                
        del self.games[match_id]

orchestrator = LudoOrchestrator()
