import asyncio
import logging
import json
import random
import uuid
from decimal import Decimal
from typing import Dict, List, Optional
from core.config import settings
from redis import asyncio as aioredis
from core.database import SessionLocal
from sqlalchemy.future import select
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
from models.user import User
from models.wallet import WalletTransaction

logger = logging.getLogger("GamerzAdda.matchmaker")


def _format_deduction_marker(deductions: dict) -> str:
    return (
        f"DEDUCT_BONUS:{to_money(deductions.get(WALLET_BUCKET_BONUS, ZERO_MONEY))};"
        f"DEDUCT_DEPOSIT:{to_money(deductions.get(WALLET_BUCKET_DEPOSIT, ZERO_MONEY))};"
        f"DEDUCT_WINNING:{to_money(deductions.get(WALLET_BUCKET_WINNING, ZERO_MONEY))}"
    )


def _parse_deductions_payload(payload: dict | None) -> dict[str, Decimal]:
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

class QuizMatchmaker:
    def __init__(self):
        self.match_pools: Dict[int, List[Dict]] = {} # entry_fee -> list of users (in-memory fallback)
        self.is_redis_active = False

    async def _get_battle_config(self, db) -> tuple[int, float, bool, int]:
        """Fetch current entry fee, prize pool, and bot settings from DB with fallbacks."""
        from models.config import SystemConfig
        configs = await db.execute(select(SystemConfig).where(SystemConfig.config_key.in_(["battle_entry_fee", "battle_prize_amount", "bot_enabled", "bot_join_delay"])))
        config_map = {c.config_key: c.config_value for c in configs.scalars().all()}
        
        entry_fee = int(config_map.get("battle_entry_fee", 36))
        prize_pool = float(config_map.get("battle_prize_amount", entry_fee * 1.8))
        bot_enabled = config_map.get("bot_enabled", "True") == "True"
        bot_join_delay = int(config_map.get("bot_join_delay", 10))
        
        return entry_fee, prize_pool, bot_enabled, bot_join_delay

    async def initialize(self):
        try:
            self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            self.is_redis_active = True
            logger.info("Matchmaker: Redis connection established")
        except Exception as e:
            logger.warning(f"Matchmaker: Redis connection failed ({e}). Using in-memory fallback.")
            self.is_redis_active = False
            
        # Start background matchmaking loop
        asyncio.create_task(self._matchmaking_loop())
        
    async def _matchmaking_loop(self):
        while True:
            await asyncio.sleep(1)
            # Always run in-memory matchmaking loop regardless of Redis status.
            for entry_fee in list(self.match_pools.keys()):
                await self.find_match(entry_fee)

    async def add_to_pool(self, user_id: int, username: str, mmr: int, entry_fee: int, bio: str = "", profile_pic: str = ""):
        # 0. Deduct entry fee immediately
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if not user:
                logger.error(f"User {user_id} not found for pool entry")
                return

            deductions = {
                WALLET_BUCKET_BONUS: ZERO_MONEY,
                WALLET_BUCKET_DEPOSIT: ZERO_MONEY,
                WALLET_BUCKET_WINNING: ZERO_MONEY,
            }
            
            # Use configured entry fee if available, otherwise fallback to what client sent (for safety)
            current_entry_fee, _, _, _ = await self._get_battle_config(db)
            # Use the most restrictive one or just the server one
            fee_to_deduct = current_entry_fee 
            
            try:
                # Deduct immediately upon searching
                deductions = debit_wallet(
                    user, 
                    fee_to_deduct, 
                    spend_order=(WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING)
                )
                deduction_marker = _format_deduction_marker(deductions)
                db.add(WalletTransaction(
                    user_id=user.id,
                    amount=-to_money(fee_to_deduct),
                    transaction_type="QUIZ_ENTRY",
                    status="SUCCESS",
                    reference_id=f"MM-ENTRY-{uuid.uuid4().hex[:8]}",
                    remark=(
                        "1v1 Matchmaking Search Fee "
                        f"(Dep: {deductions[WALLET_BUCKET_DEPOSIT]}, Win: {deductions[WALLET_BUCKET_WINNING]})"
                    ),
                    failure_reason=f"MM_ENTRY;{deduction_marker}"
                ))
                await db.commit()
            except InsufficientWalletBalanceError:
                logger.warning(f"User {username} attempted matchmaking without sufficient balance")
                from core.websockets import manager as ws_manager
                await ws_manager.send_personal_message({
                    "type": "error",
                    "message": "Insufficient balance to start matchmaking."
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

        if self.is_redis_active:
            key = f"match_pool:{entry_fee}"
            await self.redis.sadd(key, json.dumps(user_data))
        else:
            if entry_fee not in self.match_pools:
                self.match_pools[entry_fee] = []
            # Check if user already in pool
            if not any(u["user_id"] == user_id for u in self.match_pools[entry_fee]):
                self.match_pools[entry_fee].append(user_data)

        logger.info(f"User {username} ({user_id}) joined pool for ₹{entry_fee}")
        
        # Trigger matchmaking check
        asyncio.create_task(self.find_match(entry_fee))

    async def cancel_user_matchmaking(self, user_id: int):
        """
        Handle user-initiated cancellation from the searching screen.
        Refund 70% if < 5 mins, 100% if >= 5 mins.
        """
        user_entry = None
        entry_fee_found = 0
        
        # Find user in memory pools
        for entry_fee, pool in self.match_pools.items():
            for u in pool:
                if u["user_id"] == user_id:
                    user_entry = u
                    entry_fee_found = entry_fee
                    break
            if user_entry: break
            
        if not user_entry:
            logger.warning(f"Cancellation requested for user {user_id} but not found in any pool.")
            return

        # 1. Calculate Refund (90% if < 5 mins, 100% if >= 5 mins)
        now = asyncio.get_event_loop().time()
        wait_time = now - user_entry["joined_at"]
        deductions_payload = user_entry.get("deductions") or {}
        refund_buckets = _parse_deductions_payload(deductions_payload)
        
        is_early = wait_time < 300
        cancel_fee = to_money(entry_fee_found * 0.3) if is_early else ZERO_MONEY
        
        # Calculate actual refund per bucket by deducting the penalty from winning -> deposit -> bonus
        actual_refund_buckets = {k: v for k, v in refund_buckets.items()}
        if is_early:
            fee_left = cancel_fee
            for bucket in [WALLET_BUCKET_WINNING, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_BONUS]:
                if fee_left > ZERO_MONEY and actual_refund_buckets[bucket] > ZERO_MONEY:
                    take = min(actual_refund_buckets[bucket], fee_left)
                    actual_refund_buckets[bucket] -= take
                    fee_left -= take
                    
        actual_refund_total = to_money(sum(actual_refund_buckets.values(), ZERO_MONEY))

        deduction_msg = (
            f"({'70% Early Cancel Refund' if is_early else '100% Full Refund'}; "
            f"Dep: {actual_refund_buckets[WALLET_BUCKET_DEPOSIT]}, "
            f"Win: {actual_refund_buckets[WALLET_BUCKET_WINNING]}, "
            f"Bonus: {actual_refund_buckets[WALLET_BUCKET_BONUS]})"
        )

        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                # 1. Refund the 90% (or 100%) back to wallets
                for bucket, amount in actual_refund_buckets.items():
                    if amount > ZERO_MONEY:
                        credit_wallet(user, amount, bucket)
                
                db.add(WalletTransaction(
                    user_id=user.id,
                    amount=actual_refund_total,
                    transaction_type="QUIZ_REFUND",
                    status="SUCCESS",
                    reference_id=f"MM-REFUND-{uuid.uuid4().hex[:8]}",
                    remark=f"Matchmaking Refund ₹{actual_refund_total} for ₹{entry_fee_found} entry. {deduction_msg}",
                    failure_reason=f"MM_REFUND;{_format_deduction_marker(actual_refund_buckets)}"
                ))
                
                # 2. Create a history record for the 30% penalty deduction
                if is_early:
                    penalty_buckets = {k: refund_buckets[k] - actual_refund_buckets[k] for k in refund_buckets}
                    penalty_msg = (
                        f"(Dep: {penalty_buckets[WALLET_BUCKET_DEPOSIT]}, "
                        f"Win: {penalty_buckets[WALLET_BUCKET_WINNING]}, "
                        f"Bonus: {penalty_buckets[WALLET_BUCKET_BONUS]})"
                    )
                    db.add(WalletTransaction(
                        user_id=user.id,
                        amount=-cancel_fee,
                        transaction_type="PENALTY",
                        status="SUCCESS",
                        reference_id=f"MM-PENALTY-{uuid.uuid4().hex[:8]}",
                        remark=f"30% Early Cancellation Penalty. {penalty_msg}",
                        failure_reason=f"MM_PENALTY;{_format_deduction_marker(penalty_buckets)}"
                    ))

                await db.commit()
                
                from core.websockets import manager as ws_manager
                await ws_manager.send_personal_message({
                    "type": "matchmaking_refunded",
                    "amount": float(actual_refund_total),
                    "message": f"Refunded ₹{actual_refund_total} instantly. {deduction_msg}"
                }, user_id)

        # 2. Remove from pool
        self.match_pools[entry_fee_found] = [u for u in self.match_pools[entry_fee_found] if u["user_id"] != user_id]
        logger.info(f"User {user_id} cancelled matchmaking. Refunded: {actual_refund_total} (Wait: {wait_time:.1f}s)")

    async def remove_from_pool(self, user_id: int, entry_fee: int):
        if self.is_redis_active:
            key = f"match_pool:{entry_fee}"
            # This is tricky with JSON in Sets. We'd usually use a Hash or ZSet for better indexing.
            # For simplicity in this demo, we'll iterate or use a mapping.
            # In a real big app, we'd use a different Redis structure.
            pass
        else:
            if entry_fee in self.match_pools:
                self.match_pools[entry_fee] = [u for u in self.match_pools[entry_fee] if u["user_id"] != user_id]

    async def find_match(self, entry_fee: int):
        if self.is_redis_active:
            # Redis logic for ELO matchmaking...
            pass
        else:
            pool = self.match_pools.get(entry_fee, [])
            if not pool:
                return

            now = asyncio.get_event_loop().time()
            
            # 1. Try to find a human match first (Prioritize any human pair)
            if len(pool) >= 2:
                # Sort by joined_at to match the longest waiters
                pool.sort(key=lambda x: x["joined_at"])
                u1 = pool[0]
                u2 = pool[1]
                
                self.match_pools[entry_fee].remove(u1)
                self.match_pools[entry_fee].remove(u2)
                await self.create_battle(u1, u2, entry_fee)
                return
            
            # 2. If only one human, check for BOT trigger
            if len(pool) == 1:
                user = pool[0]
                wait_time = now - user["joined_at"]
                
                async with SessionLocal() as db:
                    _, _, bot_enabled, bot_join_delay = await self._get_battle_config(db)
                
                if bot_enabled and wait_time > bot_join_delay:
                    logger.info(f"Matchmaking Timeout for {user['username']} (waited {wait_time:.1f}s). Spawning BOT.")
                    self.match_pools[entry_fee].remove(user)
                    
                    from services.bot_manager import bot_manager
                    bot = await bot_manager.get_random_bot()
                    await self.create_battle(user, bot, entry_fee, is_bot=True)
                    return

    async def create_battle(self, u1: Dict, u2: Dict, entry_fee: int, is_bot: bool = False):
        battle_id = f"battle_{uuid.uuid4().hex[:8]}"
        logger.info(f"BATTLE CREATED: {battle_id} | {u1['username']} vs {u2['username']} {'(BOT)' if is_bot else ''}")
        
        # Money was already deducted at add_to_pool.
        # No extra deduction needed here.
        
        # 1. Create a VIRTUAL QuizMatch for this 1v1 Battle
        quiz_id = 0
        async with SessionLocal() as db:
            from models.quiz import QuizMatch, QuizQuestion, QuizParticipant
            from sqlalchemy import func
            from datetime import datetime, timedelta, timezone
            start_delay = datetime.now(timezone.utc) + timedelta(seconds=8)
            
            # Fetch current configured prize pool
            _, prize_pool, _, _ = await self._get_battle_config(db)
            
            new_quiz = QuizMatch(
                title=f"1v1 Battle: {u1['username']} vs {u2['username']}",
                entry_fee=entry_fee,
                prize_pool=prize_pool, 
                status="LIVE",
                match_type="BATTLE",
                start_time=start_delay,
                questions_per_quiz=10,
                time_per_question=10
            )
            db.add(new_quiz)
            await db.flush() # Get ID
            quiz_id = new_quiz.id

            # Create Participants
            p1 = QuizParticipant(quiz_id=quiz_id, user_id=u1["user_id"])
            p2 = QuizParticipant(quiz_id=quiz_id, user_id=u2["user_id"])
            db.add_all([p1, p2])

            # 2. Pick 10 random questions from BATTLE_1V1 pool
            q_res = await db.execute(
                select(QuizQuestion)
                .where(QuizQuestion.category == "BATTLE_1V1")
                .order_by(func.random())
                .limit(10)
            )
            master_questions = q_res.scalars().all()
            
            # If no BATTLE_1V1 questions, fallback to ARENA
            if not master_questions:
                logger.warning("No BATTLE_1V1 questions found! Falling back to ARENA questions.")
                q_res = await db.execute(
                    select(QuizQuestion)
                    .where(QuizQuestion.category == "ARENA")
                    .order_by(func.random())
                    .limit(10)
                )
                master_questions = q_res.scalars().all()

            # 3. Clone questions for this specific match
            for mq in master_questions:
                cloned_q = QuizQuestion(
                    quiz_id=quiz_id,
                    category="BATTLE_INSTANCE",
                    question_text=mq.question_text,
                    question_image_url=mq.question_image_url,
                    options=mq.options,
                    option_images=mq.option_images,
                    correct_option_index=mq.correct_option_index,
                    time_limit=10
                )
                db.add(cloned_q)
            
            await db.commit()

            # Tag the latest matchmaking entry transactions so draw refunds can map to the right quiz.
            for participant_id in (u1["user_id"], u2["user_id"]):
                entry_res = await db.execute(
                    select(WalletTransaction)
                    .where(WalletTransaction.user_id == participant_id)
                    .where(WalletTransaction.transaction_type == "QUIZ_ENTRY")
                    .where(WalletTransaction.status == "SUCCESS")
                    .order_by(WalletTransaction.id.desc())
                    .limit(1)
                )
                entry_tx = entry_res.scalar_one_or_none()
                if entry_tx:
                    marker = entry_tx.failure_reason or ""
                    if f"QUIZ_ID:{quiz_id}" not in marker:
                        suffix = f";QUIZ_ID:{quiz_id}"
                        entry_tx.failure_reason = (marker + suffix).strip(";")
                        db.add(entry_tx)
            await db.commit()
        
        from core.websockets import manager as ws_manager
        
        # Notify User 1
        payload = {
            "type": "battle_found",
            "battle_id": battle_id,
            "quiz_id": quiz_id,
            "entry_fee": entry_fee,
            "opponent": {
                "user_id": u2["user_id"],
                "username": u2["username"],
                "mmr": u2["mmr"],
                "bio": u2.get("bio", "I'm a bot!"),
                "profile_pic": u2.get("profile_pic") or f"{settings.APP_URL}/static/avatars/avatar{(u2['user_id'] % 5) + 1}.png",
                "is_bot": is_bot
            }
        }
        await ws_manager.send_personal_message(payload, u1["user_id"])
        
        if not is_bot:
            # Notify User 2 (only if human)
            payload["opponent"] = {
                "user_id": u1["user_id"],
                "username": u1["username"],
                "mmr": u1["mmr"],
                "bio": u1.get("bio", ""),
                "profile_pic": u1.get("profile_pic"),
                "is_bot": False
            }
            await ws_manager.send_personal_message(payload, u2["user_id"])
        
        # Trigger immediate start via orchestrator instead of waiting for loop
        from services.quiz_orchestrator import orchestrator
        asyncio.create_task(orchestrator.run_quiz_session(quiz_id))

        if is_bot:
            # Start Bot Simulation
            from services.bot_manager import bot_manager
            asyncio.create_task(bot_manager.simulate_bot_game(battle_id, u2["user_id"], quiz_id))

matchmaker = QuizMatchmaker()
