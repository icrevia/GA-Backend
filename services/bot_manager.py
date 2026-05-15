import asyncio
import random
import logging
from datetime import datetime
from sqlalchemy import select
from models.quiz import QuizQuestion, QuizResponse, QuizMatch, QuizParticipant
from models.user import User
from core.database import SessionLocal
from core.websockets import manager as ws_manager
from sqlalchemy import update

logger = logging.getLogger("GamerzAdda.bot_manager")

class BotManager:
    def __init__(self):
        self.bot_names = [
            "Rahul_Pro", "Aman_Gamer", "Priya_Quiz", "Sandeep_77", "Vikram_Adda",
            "Sonia_Play", "Deepak_King", "Anjali_Win", "Rohan_Master", "Karan_99",
            "Ishita_Pro", "Sameer_Adda", "Pooja_X", "Manish_Boss", "Neha_Gamer"
        ]

    def get_random_bot(self):
        return {
            "user_id": random.randint(99000, 99999), # Special range for bots
            "username": random.choice(self.bot_names),
            "mmr": random.randint(1100, 1400)
        }

    async def ensure_bot_users(self):
        """Pre-populates the database with bot users in the 99000-99999 range."""
        logger.info("Bot Manager: Ensuring bot users exist (range 99000-99999)...")
        async with SessionLocal() as db:
            # We don't want to check 1000 IDs one by one if possible, 
            # but for a one-time startup task, a batch check is fine.
            res = await db.execute(select(User.id).where(User.id >= 99000, User.id <= 99999))
            existing_ids = set(res.scalars().all())
            
            to_add = []
            for bot_id in range(99000, 100000):
                if bot_id not in existing_ids:
                    # Pick a base name and add suffix for uniqueness
                    base_name = random.choice(self.bot_names)
                    to_add.append(User(
                        id=bot_id,
                        username=f"{base_name}_{bot_id}",
                        email=f"bot_{bot_id}@gamerzadda.in",
                        mmr=random.randint(1100, 1500),
                        is_active=True,
                        role="USER"
                    ))
            
            if to_add:
                logger.info(f"Bot Manager: Creating {len(to_add)} bot users...")
                db.add_all(to_add)
                try:
                    await db.commit()
                    logger.info("Bot Manager: Bot users created successfully ✅")
                except Exception as e:
                    await db.rollback()
                    logger.error(f"Bot Manager: Failed to create bot users: {e}")
            else:
                logger.info("Bot Manager: All bot users already exist ✅")

    async def simulate_bot_game(self, battle_id: str, bot_user_id: int, quiz_id: int):
        """
        Simulates a bot playing a 1v1 battle. 
        Rigged: Bot always answers correctly and fast (0.8s - 1.8s).
        """
        logger.info(f"Bot {bot_user_id} starting simulation for battle {battle_id} (Quiz {quiz_id})")
        
        # Wait a bit for the match to start on client side
        await asyncio.sleep(2)

        async with SessionLocal() as db:
            # 1. Fetch questions for this quiz
            q_res = await db.execute(
                select(QuizQuestion)
                .where(QuizQuestion.quiz_id == quiz_id)
                .order_by(QuizQuestion.id.asc())
            )
            questions = q_res.scalars().all()
            
            # 2. Fetch quiz config for timing
            quiz_res = await db.execute(select(QuizMatch).where(QuizMatch.id == quiz_id))
            quiz = quiz_res.scalar_one_or_none()
            time_per_q = quiz.time_per_question if (quiz and quiz.time_per_question) else 5

            if not questions:
                logger.error(f"Bot Manager: No questions found for quiz {quiz_id}")
                return

            for q in questions:
                # Bot 'thinks' and 'responds'
                # Rigged for speed: 0.8s to 1.8s
                response_time = random.uniform(800, 1800)
                await asyncio.sleep(response_time / 1000.0)

                # Record correct response in DB
                try:
                    # Ensure participant exists (failsafe)
                    p_res = await db.execute(
                        select(QuizParticipant).where(
                            QuizParticipant.quiz_id == quiz_id,
                            QuizParticipant.user_id == bot_user_id
                        )
                    )
                    if not p_res.scalar_one_or_none():
                        db.add(QuizParticipant(quiz_id=quiz_id, user_id=bot_user_id))
                        await db.flush()

                    bot_ans = QuizResponse(
                        quiz_id=quiz_id,
                        question_id=q.id,
                        user_id=bot_user_id,
                        option_index=q.correct_option_index,
                        is_correct=True,
                        response_time_ms=int(response_time)
                    )
                    db.add(bot_ans)
                    await db.commit()
                except Exception as e:
                    await db.rollback() # CRITICAL: Reset session state after error
                    logger.error(f"Bot Manager Error recording answer: {e}")
                    # Continue loop, next iteration will have a fresh transaction state
                
                # Wait for next question interval
                wait_next = max(0.1, time_per_q - (response_time / 1000.0))
                await asyncio.sleep(wait_next)

            # Mark BOT as completed
            try:
                await db.execute(
                    update(QuizParticipant)
                    .where(QuizParticipant.quiz_id == quiz_id, QuizParticipant.user_id == bot_user_id)
                    .values(status="COMPLETED")
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"Bot Manager Error marking completed: {e}")

            # Check if Battle results can be calculated
            from services.quiz_orchestrator import orchestrator
            asyncio.create_task(orchestrator.process_battle_results(quiz_id))

        logger.info(f"Bot {bot_user_id} finished battle {battle_id}")

bot_manager = BotManager()
