import asyncio
import logging
from sqlalchemy import select, update
import uuid

from core.database import SessionLocal
from core.websockets import manager
from models.rps import RPSMatch, RPSParticipant
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import credit_wallet, WALLET_BUCKET_WINNING
from services.rps_engine import RPSEngine

logger = logging.getLogger("GamerzAdda.rps_orchestrator")

class RPSOrchestrator:
    def __init__(self):
        self.games: dict[int, RPSEngine] = {}
        self.timers: dict[int, asyncio.TimerHandle] = {}

    async def _broadcast(self, match_id: int, engine: RPSEngine):
        payload = {"type": "RPS_STATE", "payload": engine.get_state()}
        for uid in engine.players:
            await manager.send_personal_message(payload, uid)

    def start_game(self, match_id: int, player1_id: int, player2_id: int):
        engine = RPSEngine(match_id, [player1_id, player2_id], duration_seconds=10)
        self.games[match_id] = engine
        
        loop = asyncio.get_running_loop()
        self.timers[match_id] = loop.call_later(11.0, lambda: asyncio.create_task(self._handle_timeout(match_id)))
        
        asyncio.create_task(self._broadcast(match_id, engine))

    async def handle_action(self, match_id: int, user_id: int, action: dict):
        engine = self.games.get(match_id)
        if not engine:
            return

        action_type = action.get("action")
        if action_type == "submit_move":
            move = action.get("move")
            success = engine.submit_move(user_id, move)
            if success:
                await self._broadcast(match_id, engine)
                
                if engine.all_moves_submitted():
                    # Both submitted, evaluate early
                    timer = self.timers.pop(match_id, None)
                    if timer:
                        timer.cancel()
                    await self._evaluate_and_finish(match_id, engine)

    async def _handle_timeout(self, match_id: int):
        engine = self.games.get(match_id)
        if not engine or engine.state != "COUNTDOWN":
            return
            
        await self._evaluate_and_finish(match_id, engine)

    async def _evaluate_and_finish(self, match_id: int, engine: RPSEngine):
        engine.evaluate_result()
        await self._broadcast(match_id, engine)
        
        # Hold reveal for 3 seconds before finishing
        await asyncio.sleep(3.0)
        
        engine.state = "COMPLETED"
        await self._broadcast(match_id, engine)
        
        # Settle in database
        try:
            async with SessionLocal() as db:
                match_res = await db.execute(select(RPSMatch).where(RPSMatch.id == match_id))
                match_obj = match_res.scalar_one_or_none()
                if not match_obj:
                    return

                if engine.is_draw:
                    match_obj.status = "REFUNDED"
                    # Refund entry fee to both
                    refund_amount = match_obj.entry_fee
                    for uid in engine.players:
                        user_obj = await db.get(User, uid)
                        if user_obj:
                            credit_wallet(user_obj, refund_amount, WALLET_BUCKET_WINNING)
                            db.add(WalletTransaction(
                                user_id=uid,
                                amount=refund_amount,
                                transaction_type="PRIZE_WIN",
                                status="SUCCESS",
                                reference_id=f"RPS-DRAW-{match_id}-{uid}",
                                payment_mode="RPS_REFUND"
                            ))
                            db.add(user_obj)
                            
                        # Update participant
                        await db.execute(
                            update(RPSParticipant)
                            .where(RPSParticipant.match_id == match_id, RPSParticipant.user_id == uid)
                            .values(status="DRAW", move=engine.moves.get(uid))
                        )

                else:
                    match_obj.status = "COMPLETED"
                    match_obj.winner_id = engine.winner_id
                    
                    winner_obj = await db.get(User, engine.winner_id)
                    if winner_obj:
                        credit_wallet(winner_obj, match_obj.prize_pool, WALLET_BUCKET_WINNING)
                        db.add(WalletTransaction(
                            user_id=engine.winner_id,
                            amount=match_obj.prize_pool,
                            transaction_type="PRIZE_WIN",
                            status="SUCCESS",
                            reference_id=f"RPS-WIN-{match_id}-{engine.winner_id}",
                            payment_mode="RPS_WIN"
                        ))
                        db.add(winner_obj)
                        
                    for uid in engine.players:
                        p_status = "WON" if uid == engine.winner_id else "LOST"
                        await db.execute(
                            update(RPSParticipant)
                            .where(RPSParticipant.match_id == match_id, RPSParticipant.user_id == uid)
                            .values(status=p_status, move=engine.moves.get(uid))
                        )

                await db.commit()
                
        except Exception as e:
            logger.error(f"Error settling RPS match {match_id}: {e}")
            
        finally:
            self.games.pop(match_id, None)


orchestrator = RPSOrchestrator()
