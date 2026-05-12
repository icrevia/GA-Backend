import asyncio
import logging
import json
import random
import uuid
from typing import Dict, List, Optional
from core.config import settings
from redis import asyncio as aioredis

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
            # Redis logic for ELO matchmaking
            # 1. Fetch all users in pool
            # 2. Find two with closest MMR
            # 3. Create battle
            pass
        else:
            pool = self.match_pools.get(entry_fee, [])
            if len(pool) < 2:
                return

            # Sort by MMR to find closest matches
            pool.sort(key=lambda x: x["mmr"])
            
            for i in range(len(pool) - 1):
                u1 = pool[i]
                u2 = pool[i+1]
                
                # ELO Matchmaking: Only match if MMR difference is small (e.g., < 200)
                # Or if they've been waiting for too long
                mmr_diff = abs(u1["mmr"] - u2["mmr"])
                wait_time = asyncio.get_event_loop().time() - min(u1["joined_at"], u2["joined_at"])
                
                if mmr_diff < 200 or wait_time > 15:
                    # MATCH FOUND!
                    self.match_pools[entry_fee].remove(u1)
                    self.match_pools[entry_fee].remove(u2)
                    await self.create_battle(u1, u2, entry_fee)
                    return

    async def create_battle(self, u1: Dict, u2: Dict, entry_fee: int):
        battle_id = f"battle_{uuid.uuid4().hex[:8]}"
        logger.info(f"BATTLE CREATED: {battle_id} | {u1['username']} vs {u2['username']}")
        
        from core.websockets import manager as ws_manager
        
        payload = {
            "type": "battle_found",
            "battle_id": battle_id,
            "entry_fee": entry_fee,
            "opponent": {
                "user_id": u2["user_id"],
                "username": u2["username"],
                "mmr": u2["mmr"]
            }
        }
        await ws_manager.send_personal_message(payload, u1["user_id"])
        
        payload["opponent"] = {
            "user_id": u1["user_id"],
            "username": u1["username"],
            "mmr": u1["mmr"]
        }
        await ws_manager.send_personal_message(payload, u2["user_id"])

matchmaker = QuizMatchmaker()
