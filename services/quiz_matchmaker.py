import asyncio
import logging
import json
import random
import uuid
from typing import Dict, List, Optional
from core.config import settings
from redis import asyncio as aioredis
from core.database import SessionLocal
from sqlalchemy.future import select

logger = logging.getLogger("GamerzAdda.matchmaker")

class QuizMatchmaker:
    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.match_pools: Dict[int, List[Dict]] = {} # entry_fee -> list of users (in-memory fallback)
        self.is_redis_active = False

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
            if not self.is_redis_active:
                for entry_fee in list(self.match_pools.keys()):
                    await self.find_match(entry_fee)

    async def add_to_pool(self, user_id: int, username: str, mmr: int, entry_fee: int):
        user_data = {
            "user_id": user_id,
            "username": username,
            "mmr": mmr,
            "entry_fee": entry_fee,
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
            
            # 1. Try to find a human match first
            if len(pool) >= 2:
                pool.sort(key=lambda x: x["mmr"])
                for i in range(len(pool) - 1):
                    u1 = pool[i]
                    u2 = pool[i+1]
                    mmr_diff = abs(u1["mmr"] - u2["mmr"])
                    wait_time = now - min(u1["joined_at"], u2["joined_at"])
                    
                    if mmr_diff < 200 or wait_time > 15:
                        self.match_pools[entry_fee].remove(u1)
                        self.match_pools[entry_fee].remove(u2)
                        await self.create_battle(u1, u2, entry_fee)
                        return

            # 2. If no human match, check for BOT trigger (wait > 8s)
            for user in pool[:]:
                wait_time = now - user["joined_at"]
                if wait_time > 8:
                    logger.info(f"Matchmaking Timeout for {user['username']} (waited {wait_time:.1f}s). Spawning BOT.")
                    self.match_pools[entry_fee].remove(user)
                    
                    from services.bot_manager import bot_manager
                    bot = bot_manager.get_random_bot()
                    await self.create_battle(user, bot, entry_fee, is_bot=True)
                    return

    async def create_battle(self, u1: Dict, u2: Dict, entry_fee: int, is_bot: bool = False):
        battle_id = f"battle_{uuid.uuid4().hex[:8]}"
        logger.info(f"BATTLE CREATED: {battle_id} | {u1['username']} vs {u2['username']} {'(BOT)' if is_bot else ''}")
        
        # 1. Create a VIRTUAL QuizMatch for this 1v1 Battle
        quiz_id = 0
        async with SessionLocal() as db:
            from models.quiz import QuizMatch, QuizQuestion
            from sqlalchemy import func
            
            new_quiz = QuizMatch(
                title=f"1v1 Battle: {u1['username']} vs {u2['username']}",
                entry_fee=entry_fee,
                prize_pool=entry_fee * 1.8, # 10% platform fee
                status="LIVE",
                match_type="BATTLE",
                start_time=func.now(),
                questions_per_quiz=10,
                time_per_question=10
            )
            db.add(new_quiz)
            await db.flush() # Get ID
            quiz_id = new_quiz.id

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
                "is_bot": False
            }
            await ws_manager.send_personal_message(payload, u2["user_id"])
        else:
            # Start Bot Simulation
            from services.bot_manager import bot_manager
            asyncio.create_task(bot_manager.simulate_bot_game(battle_id, u2["user_id"], quiz_id))

matchmaker = QuizMatchmaker()
