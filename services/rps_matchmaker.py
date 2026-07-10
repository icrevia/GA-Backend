import asyncio
import logging
from typing import Dict, List
import time
from sqlalchemy import select

from core.database import SessionLocal
from core.websockets import manager
from models.user import User
from models.rps import RPSMatch, RPSParticipant
from models.wallet import WalletTransaction
from services.wallet_balances import debit_wallet, get_wallet_breakdown, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING, WALLET_BUCKET_BONUS
from services.rps_orchestrator import orchestrator

logger = logging.getLogger("GamerzAdda.rps_matchmaker")

class RPSMatchmaker:
    def __init__(self):
        # fee -> list of {user_id, joined_at}
        self.pools: Dict[int, List[dict]] = {}
        self.lock = asyncio.Lock()

    async def add_to_pool(self, user_id: int, entry_fee: int):
        if entry_fee <= 0:
            return

        async with self.lock:
            if entry_fee not in self.pools:
                self.pools[entry_fee] = []

            # Check if already in pool
            for pool_fee, pool in self.pools.items():
                for entry in pool:
                    if entry["user_id"] == user_id:
                        return # Already in some pool

            self.pools[entry_fee].append({
                "user_id": user_id,
                "joined_at": time.time()
            })
            
        asyncio.create_task(self._try_match(entry_fee))

    async def cancel_matchmaking(self, user_id: int):
        async with self.lock:
            for fee, pool in self.pools.items():
                for i, entry in enumerate(pool):
                    if entry["user_id"] == user_id:
                        pool.pop(i)
                        return

    async def _try_match(self, entry_fee: int):
        async with self.lock:
            pool = self.pools.get(entry_fee, [])
            if len(pool) < 2:
                return

            p1 = pool.pop(0)
            p2 = pool.pop(0)

        u1_id = p1["user_id"]
        u2_id = p2["user_id"]
        
        # We need to deduct fee and create match in DB
        # To avoid blocking lock, we do it outside lock
        
        prize_multiplier = 1.8 # This should come from config, hardcoded for now
        prize_pool = entry_fee * prize_multiplier
        
        try:
            async with SessionLocal() as db:
                u1 = await db.get(User, u1_id)
                u2 = await db.get(User, u2_id)
                
                # Check balances (we simplify debit logic for brevity in this task, but normally we use exact buckets)
                wb1 = get_wallet_breakdown(u1)["balance"]
                wb2 = get_wallet_breakdown(u2)["balance"]
                
                if wb1 < entry_fee or wb2 < entry_fee:
                    logger.warning(f"Insufficient funds for RPS matchmaking: {u1_id} or {u2_id}")
                    # Re-queue the one who has funds
                    if wb1 >= entry_fee:
                        asyncio.create_task(self.add_to_pool(u1_id, entry_fee))
                    if wb2 >= entry_fee:
                        asyncio.create_task(self.add_to_pool(u2_id, entry_fee))
                    return

                # Debit both
                debit_wallet(u1, entry_fee, spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING))
                debit_wallet(u2, entry_fee, spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING))
                
                db.add(u1)
                db.add(u2)

                # Create match
                match_obj = RPSMatch(
                    entry_fee=entry_fee,
                    prize_pool=prize_pool,
                    status="PLAYING"
                )
                db.add(match_obj)
                await db.flush()
                
                # Create transactions
                db.add(WalletTransaction(user_id=u1_id, amount=-entry_fee, transaction_type="JOIN_TOURNAMENT", status="SUCCESS", reference_id=f"RPS-JOIN-{match_obj.id}-{u1_id}"))
                db.add(WalletTransaction(user_id=u2_id, amount=-entry_fee, transaction_type="JOIN_TOURNAMENT", status="SUCCESS", reference_id=f"RPS-JOIN-{match_obj.id}-{u2_id}"))

                # Create participants
                p1_obj = RPSParticipant(match_id=match_obj.id, user_id=u1_id)
                p2_obj = RPSParticipant(match_id=match_obj.id, user_id=u2_id)
                db.add_all([p1_obj, p2_obj])
                
                await db.commit()
                
                # Notify players
                for uid in [u1_id, u2_id]:
                    opp_id = u2_id if uid == u1_id else u1_id
                    opp_user = u2 if uid == u1_id else u1
                    await manager.send_personal_message({
                        "type": "rps_match_found",
                        "match_id": match_obj.id,
                        "opponent": {
                            "user_id": opp_user.id,
                            "username": opp_user.username,
                            "profile_pic": opp_user.profile_pic or ""
                        }
                    }, uid)
                    
                # Start engine
                orchestrator.start_game(match_obj.id, u1_id, u2_id)
                
        except Exception as e:
            logger.error(f"Error in RPS Matchmaking DB commit: {e}")

matchmaker = RPSMatchmaker()
