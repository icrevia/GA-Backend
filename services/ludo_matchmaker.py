import asyncio
import logging
import json
import uuid
from typing import Dict, List
from decimal import Decimal
from core.config import settings
from redis import asyncio as aioredis
from core.database import SessionLocal
from sqlalchemy.future import select
from models.user import User
from models.wallet import WalletTransaction
from models.ludo import LudoMatch, LudoParticipant
from services.wallet_balances import (
    debit_wallet,
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    ZERO_MONEY,
    to_money,
    InsufficientWalletBalanceError,
    credit_wallet,
)
from services.bot_manager import bot_manager

logger = logging.getLogger("GamerzAdda.LudoMatchmaker")

def _format_deduction_marker(deductions: dict) -> str:
    return (
        f"DEDUCT_BONUS:{to_money(deductions.get(WALLET_BUCKET_BONUS, ZERO_MONEY))};"
        f"DEDUCT_DEPOSIT:{to_money(deductions.get(WALLET_BUCKET_DEPOSIT, ZERO_MONEY))};"
        f"DEDUCT_WINNING:{to_money(deductions.get(WALLET_BUCKET_WINNING, ZERO_MONEY))}"
    )

def _parse_deductions_payload(payload: dict | None) -> dict:
    from decimal import Decimal
    parsed = {
        WALLET_BUCKET_BONUS: ZERO_MONEY,
        WALLET_BUCKET_DEPOSIT: ZERO_MONEY,
        WALLET_BUCKET_WINNING: ZERO_MONEY,
    }
    if not payload:
        return parsed
    for bucket in parsed.keys():
        raw = payload.get(bucket)
        if raw is None:
            continue
        try:
            parsed[bucket] = to_money(raw)
        except Exception:
            parsed[bucket] = ZERO_MONEY
    return parsed

class LudoMatchmaker:
    def __init__(self):
        self.match_pools: Dict[int, List[Dict]] = {} # entry_fee -> list of users
        self.is_redis_active = False

    async def initialize(self):
        try:
            self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            self.is_redis_active = True
            logger.info("Ludo Matchmaker: Redis connection established")
        except Exception as e:
            logger.warning(f"Ludo Matchmaker: Redis connection failed ({e}). Using in-memory fallback.")
            self.is_redis_active = False
            
        asyncio.create_task(self._matchmaking_loop())
        
    async def _matchmaking_loop(self):
        while True:
            await asyncio.sleep(1)
            # Always run in-memory matchmaking loop regardless of Redis status.
            # Redis is only used for cross-process shared state (future);
            # for single-process deployments, in-memory pools always need processing.
            for entry_fee in list(self.match_pools.keys()):
                await self.find_match(entry_fee)

    async def add_to_pool(self, user_id: int, username: str, mmr: int, entry_fee: int, bio: str = "", profile_pic: str = ""):
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if not user:
                return

            try:
                deductions = {}
                if entry_fee > 0:
                    from models.config import SystemConfig
                    import json
                    res = await db.execute(select(SystemConfig).where(SystemConfig.key == "ludo_config"))
                    row = res.scalar_one_or_none()
                    bonus_pct = 0
                    if row and row.value:
                        stored = json.loads(row.value) if isinstance(row.value, str) else row.value
                        bonus_pct = stored.get("bonus_usage_percentage", 0)
                    max_b = Decimal(entry_fee) * (Decimal(bonus_pct) / Decimal(100))
                    
                    deductions = debit_wallet(
                        user, 
                        entry_fee, 
                        spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING),
                        max_bonus_amount=max_b
                    )
                    deduction_marker = _format_deduction_marker(deductions)
                    db.add(WalletTransaction(
                        user_id=user.id,
                        amount=-to_money(entry_fee),
                        transaction_type="LUDO_ENTRY",
                        status="SUCCESS",
                        reference_id=f"LMM-ENTRY-{uuid.uuid4().hex[:8]}",
                        remark=f"Ludo Matchmaking Search Fee",
                        failure_reason=f"LMM_ENTRY;{deduction_marker}"
                    ))
                    await db.commit()
            except InsufficientWalletBalanceError:
                from core.websockets import manager as ws_manager
                await ws_manager.send_personal_message({
                    "type": "error",
                    "message": "Insufficient balance to play Ludo."
                }, user_id)
                return

        user_data = {
            "user_id": user_id,
            "username": username,
            "mmr": mmr,
            "bio": bio,
            "profile_pic": profile_pic,
            "entry_fee": entry_fee,
            "deductions": {k: str(to_money(v)) for k, v in deductions.items()},
            "joined_at": asyncio.get_event_loop().time()
        }

        if entry_fee not in self.match_pools:
            self.match_pools[entry_fee] = []
        if not any(u["user_id"] == user_id for u in self.match_pools[entry_fee]):
            self.match_pools[entry_fee].append(user_data)

        logger.info(f"User {username} joined Ludo pool for ₹{entry_fee}")
        
    async def cancel_user_matchmaking(self, user_id: int):
        user_entry = None
        entry_fee_found = 0
        
        for entry_fee, pool in self.match_pools.items():
            for u in pool:
                if u["user_id"] == user_id:
                    user_entry = u
                    entry_fee_found = entry_fee
                    break
            if user_entry: break
            
        if not user_entry: return

        now = asyncio.get_event_loop().time()
        wait_time = now - user_entry["joined_at"]
        deductions_payload = user_entry.get("deductions") or {}
        refund_buckets = _parse_deductions_payload(deductions_payload)
        
        from decimal import Decimal
        is_early = wait_time < 300
        cancel_fee = to_money(entry_fee_found * 0.3) if is_early else ZERO_MONEY
        
        actual_refund_buckets = {k: v for k, v in refund_buckets.items()}
        if is_early:
            fee_left = cancel_fee
            for bucket in [WALLET_BUCKET_WINNING, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_BONUS]:
                if fee_left > ZERO_MONEY and actual_refund_buckets.get(bucket, ZERO_MONEY) > ZERO_MONEY:
                    take = min(actual_refund_buckets[bucket], fee_left)
                    actual_refund_buckets[bucket] -= take
                    fee_left -= take
                    
        actual_refund_total = to_money(sum(actual_refund_buckets.values(), ZERO_MONEY))
        penalty_msg = "70% early abort" if is_early else "100% full refund"

        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                for bucket, amount in actual_refund_buckets.items():
                    if amount > ZERO_MONEY:
                        credit_wallet(user, amount, bucket)
                
                db.add(WalletTransaction(
                    user_id=user.id,
                    amount=actual_refund_total,
                    transaction_type="LUDO_REFUND",
                    status="SUCCESS",
                    reference_id=f"LMM-REFUND-{uuid.uuid4().hex[:8]}",
                    remark=f"Ludo Matchmaking Refund ({penalty_msg})",
                ))
                await db.commit()

        self.match_pools[entry_fee_found] = [u for u in self.match_pools[entry_fee_found] if u["user_id"] != user_id]
        from core.websockets import manager as ws_manager
        await ws_manager.send_personal_message({
            "type": "ludo_matchmaking_cancelled",
            "message": "Matchmaking cancelled."
        }, user_id)

    async def find_match(self, entry_fee: int):
        pool = self.match_pools.get(entry_fee, [])
        if not pool: return

        now = asyncio.get_event_loop().time()
        
        if len(pool) >= 2:
            pool.sort(key=lambda x: x["joined_at"])
            u1, u2 = pool[0], pool[1]
            self.match_pools[entry_fee].remove(u1)
            self.match_pools[entry_fee].remove(u2)
            await self.create_battle(u1, u2, entry_fee)
            return
        
        if len(pool) == 1:
            user = pool[0]
            wait_time = now - user["joined_at"]
            if wait_time > 10:
                logger.info(f"Ludo Timeout for {user['username']}. Spawning BOT.")
                self.match_pools[entry_fee].remove(user)
                bot = await bot_manager.get_random_bot()
                await self.create_battle(user, bot, entry_fee, is_bot=True)
                return

    async def create_battle(self, u1: Dict, u2: Dict, entry_fee: int, is_bot: bool = False):
        async with SessionLocal() as db:
            from models.config import SystemConfig
            import json
            res = await db.execute(select(SystemConfig).where(SystemConfig.key == "ludo_config"))
            row = res.scalar_one_or_none()
            prize_mult = 1.8
            if row and row.value:
                stored = json.loads(row.value) if isinstance(row.value, str) else row.value
                prize_mult = float(stored.get("prize_multiplier", 1.8))
            prize_pool = float(to_money(entry_fee * prize_mult))
            match = LudoMatch(
                entry_fee=entry_fee,
                prize_pool=prize_pool,
                status="PLAYING"
            )
            db.add(match)
            await db.flush()
            
            import random
            pairs = [("RED", "YELLOW"), ("GREEN", "BLUE")]
            color1, color2 = random.choice(pairs)
            if random.random() < 0.5:
                color1, color2 = color2, color1

            p1 = LudoParticipant(match_id=match.id, user_id=u1["user_id"], color=color1)
            p2 = LudoParticipant(match_id=match.id, user_id=u2["user_id"], color=color2)
            db.add_all([p1, p2])
            await db.commit()
            
            match_id = match.id

        from core.websockets import manager as ws_manager
        
        payload = {
            "type": "ludo_match_found",
            "match_id": str(match_id),
            "entry_fee": entry_fee,
            "your_color": color1,
            "opponent": {
                "user_id": u2["user_id"],
                "username": u2["username"],
                "profile_pic": u2.get("profile_pic", ""),
                "is_bot": is_bot
            }
        }
        await ws_manager.send_personal_message(payload, u1["user_id"])
        
        if not is_bot:
            payload["your_color"] = color2
            payload["opponent"] = {
                "user_id": u1["user_id"],
                "username": u1["username"],
                "profile_pic": u1.get("profile_pic", ""),
                "is_bot": False
            }
            await ws_manager.send_personal_message(payload, u2["user_id"])
        
        from services.ludo_orchestrator import orchestrator
        await orchestrator.start_game(match_id)

        if is_bot:
            asyncio.create_task(bot_manager.simulate_ludo_bot_game(match_id, u2["user_id"]))

ludo_matchmaker = LudoMatchmaker()
