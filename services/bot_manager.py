import asyncio
import random
import logging
import uuid
from datetime import datetime
from sqlalchemy import select, func, update
from models.quiz import QuizQuestion, QuizResponse, QuizMatch, QuizParticipant
from models.user import User
from core.database import SessionLocal
from core.websockets import manager as ws_manager
from core.config import settings

logger = logging.getLogger("GamerzAdda.bot_manager")

class BotManager:
    def __init__(self):
        import os
        self.custom_names = []
        try:
            file_path = os.path.join(os.path.dirname(__file__), "bot_names.txt")
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    self.custom_names = [line.strip() for line in f if line.strip()]
        except Exception as e:
            logger.error(f"Failed to load bot names: {e}")
            
        # Indian First Names (Diverse)
        self.first_names = [
            "Aryan", "Vihaan", "Sia", "Ananya", "Kabir", "Ishaan", "Advait", "Myra", "Kyra", "Zoya",
            "Arjun", "Rohan", "Aditya", "Sameer", "Rahul", "Amit", "Sandeep", "Priya", "Neha", "Anjali",
            "Sonia", "Deepak", "Vikram", "Karan", "Manish", "Pooja", "Siddharth", "Varun", "Kartik", "Rishabh",
            "Akshay", "Abhinav", "Tushar", "Mayank", "Ayush", "Shubham", "Vivek", "Sourabh", "Sumit", "Pranjal",
            "Aarav", "Ishani", "Kavya", "Reyansh", "Atharva", "Dia", "Ishita", "Yuvraj", "Tanmay", "Ritika",
            "Harsh", "Prateek", "Gaurav", "Yash", "Sneha", "Kriti", "Bhavya", "Divya", "Ankit", "Rohit"
        ]
        
        # Gaming Suffixes
        self.suffixes = [
            "Pro", "Gaming", "God", "Adda", "King", "Winner", "Master", "77", "99", "YT",
            "Official", "Gamer", "Killer", "Legend", "Squad", "Sniper", "Striker", "Hunter", "Warrior", "Champion",
            "X", "Zero", "Bolt", "Dash", "Flash", "Shadow", "Ghost", "Phantm", "Elite", "Prime"
        ]

        # Gaming Bios
        self.bios = [
            "Always ready for a challenge! 🎮", "Born to play, forced to work. 😎", "Eat, Sleep, Game, Repeat. 🔥",
            "Losing is not an option. 🏆", "Gaming is my therapy. 🧘", "Let the games begin! 🚀",
            "Chasing the top rank. 📈", "Gaming is life. 🕹️", "Professional Noob. 😂", "Just here to win. 💰",
            "Fast fingers, fast mind. ⚡", "Quiz master in the making. 🧠", "1v1 me if you dare! ⚔️",
            "Gaming since 2015. 👴", "Mobile gaming legend. 📱", "Leveling up everyday. ⭐",
            "No lag, only skill. 🦾", "Future Esports Champ. 🎖️", "Strategist & Gamer. ♟️", "Peace out! ✌️"
        ]

    def _generate_username(self):
        fn = random.choice(self.first_names)
        sx = random.choice(self.suffixes)
        sep = random.choice(["", "_", " "])
        return f"{fn}{sep}{sx}"

    async def get_random_bot(self):
        """Returns a real bot from DB with all necessary fields."""
        async with SessionLocal() as db:
            # Pick a random bot that actually exists in our reserved range
            res = await db.execute(
                select(User)
                .where(User.id >= 99000, User.id <= 99999)
                .order_by(func.random())
                .limit(1)
            )
            bot_user = res.scalar_one_or_none()
            
            if not bot_user:
                # Fallback if DB is empty (shouldn't happen with ensure_bot_users)
                bot_id = random.randint(99000, 99999)
                return {
                    "user_id": bot_id,
                    "username": f"Bot_{bot_id}",
                    "mmr": 1200,
                    "bio": "Just a kid, earning pocket money",
                    "profile_pic": ""
                }

            return {
                "user_id": bot_user.id,
                "username": bot_user.username,
                "mmr": bot_user.mmr or 1200,
                "bio": bot_user.bio or "Always ready!",
                "profile_pic": bot_user.profile_pic
            }

    async def ensure_bot_users(self):
        """Pre-populates the database with 1000 smart bot users."""
        logger.info("Bot Manager: Ensuring 1000 smart bot users exist (range 99000-99999)...")
        async with SessionLocal() as db:
            res = await db.execute(select(User.id).where(User.id >= 99000, User.id <= 99999))
            existing_ids = set(res.scalars().all())
            
            to_add = []
            for bot_id in range(99000, 100000):
                if bot_id not in existing_ids:
                    idx = bot_id - 99000
                    if self.custom_names and idx < len(self.custom_names):
                        username = self.custom_names[idx]
                    else:
                        username = self._generate_username()
                        
                    # Ensure username uniqueness (basic check)
                    if any(u.username == username for u in to_add):
                        username = f"{username}_{bot_id}"
                        
                    avatar_idx = (bot_id % 5) + 1
                    
                    to_add.append(User(
                        id=bot_id,
                        username=username,
                        email=f"bot_{bot_id}@gamerzadda.in",
                        mmr=random.randint(1100, 1500),
                        bio=random.choice(self.bios),
                        profile_pic=f"{settings.APP_URL}/static/avatars/avatar{avatar_idx}.png",
                        is_active=True,
                        role="USER"
                    ))
            
            if to_add:
                logger.info(f"Bot Manager: Creating {len(to_add)} smart bot users...")
                # Batch add in chunks to avoid memory/lock issues
                chunk_size = 200
                for i in range(0, len(to_add), chunk_size):
                    chunk = to_add[i:i + chunk_size]
                    db.add_all(chunk)
                    await db.flush()
                
                try:
                    await db.commit()
                    logger.info("Bot Manager: 1000 smart bots created successfully ✅")
                except Exception as e:
                    await db.rollback()
                    logger.error(f"Bot Manager: Failed to create bot users: {e}")
            else:
                logger.info("Bot Manager: 1000 smart bots already exist ✅")

    async def simulate_bot_game(self, battle_id: str, bot_user_id: int, quiz_id: int):
        """Simulates a bot playing a 1v1 battle."""
        logger.info(f"Bot {bot_user_id} starting simulation for battle {battle_id} (Quiz {quiz_id})")
        await asyncio.sleep(2)

        async with SessionLocal() as db:
            q_res = await db.execute(
                select(QuizQuestion)
                .where(QuizQuestion.category == "BATTLE_1V1")
                .order_by(func.random()) # Pick random questions for the bot too
                .limit(5)
            )
            # Actually, the quiz questions are already fixed in create_battle.
            # We should fetch questions LINKED to this quiz_id.
            q_res = await db.execute(
                select(QuizQuestion)
                .join(QuizMatch.questions)
                .where(QuizMatch.id == quiz_id)
                .order_by(QuizQuestion.id.asc())
            )
            questions = q_res.scalars().all()
            
            if not questions:
                # Fallback to general questions if link is missing
                q_res = await db.execute(select(QuizQuestion).limit(5))
                questions = q_res.scalars().all()

            for q in questions:
                # Smart Bot Timing: 1.2s to 2.5s (more realistic than 0.8s)
                response_time = random.uniform(1200, 2500)
                await asyncio.sleep(response_time / 1000.0)

                try:
                    bot_ans = QuizResponse(
                        quiz_id=quiz_id,
                        question_id=q.id,
                        user_id=bot_user_id,
                        option_index=q.correct_option_index, # Bot is smart but we can add 'miss' chance later
                        is_correct=True,
                        response_time_ms=int(response_time)
                    )
                    db.add(bot_ans)
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    logger.error(f"Bot Manager Answer Error: {e}")
                
                await asyncio.sleep(0.5)

            try:
                await db.execute(
                    update(QuizParticipant)
                    .where(QuizParticipant.quiz_id == quiz_id, QuizParticipant.user_id == bot_user_id)
                    .values(status="COMPLETED")
                )
                await db.commit()
            except Exception as e:
                await db.rollback()

            from services.quiz_orchestrator import orchestrator
            asyncio.create_task(orchestrator.process_battle_results(quiz_id))

        logger.info(f"Bot {bot_user_id} finished battle")

bot_manager = BotManager()
