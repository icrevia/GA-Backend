import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from core.database import SyncSessionLocal
from models.quiz import QuizMatch, QuizQuestion, QuizParticipant
from models.user import User
from models.wallet import WalletTransaction
from core.websockets import manager as ws_manager
from services.wallet_balances import credit_wallet, WALLET_BUCKET_WINNING, to_money
from services.notifications import add_user_notification

logger = logging.getLogger("GamerzAdda.quiz")

class QuizOrchestrator:
    def __init__(self):
        self.active_quizzes = set()

    async def start(self):
        logger.info("Quiz Orchestrator started")
        while True:
            try:
                await self.check_and_start_quizzes()
            except Exception as e:
                logger.error(f"Error in Quiz Orchestrator: {e}")
            await asyncio.sleep(30)  # Check every 30 seconds

    async def check_and_start_quizzes(self):
        db = SyncSessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Find UPCOMING quizzes that should start within the next minute, 
            # OR quizzes that are already LIVE but not in our active tracker (recovery)
            to_process = db.query(QuizMatch).filter(
                (QuizMatch.status == "LIVE") | 
                ((QuizMatch.status == "UPCOMING") & (QuizMatch.start_time <= now + timedelta(seconds=60)))
            ).all()

            for quiz in to_process:
                if quiz.id not in self.active_quizzes:
                    self.active_quizzes.add(quiz.id)
                    asyncio.create_task(self.run_quiz_session(quiz.id))
        finally:
            db.close()

    async def run_quiz_session(self, quiz_id: int):
        logger.info(f"Starting quiz session for quiz_id={quiz_id}")
        
        # 1. Update status to LIVE
        db = SyncSessionLocal()
        try:
            quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
            if not quiz: return
            quiz.status = "LIVE"
            db.add(quiz)
            db.commit()

            # 2. Get questions
            questions = db.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.id.asc()).all()
            if quiz.question_pool_size:
                questions = questions[:quiz.question_pool_size]
            if not questions:
                logger.warning(f"No questions for quiz_id={quiz_id}, ending.")
                quiz.status = "COMPLETED"
                db.commit()
                return

            # 3. Broadcast quiz sync payload (question pool + settings)
            question_pool = []
            for q in questions:
                option_images = list(q.option_images or [])
                options_payload = []
                for idx, opt_text in enumerate(q.options or []):
                    image_url = option_images[idx] if idx < len(option_images) else None
                    options_payload.append({"text": opt_text, "image_url": image_url})

                question_pool.append({
                    "id": q.id,
                    "question_text": q.question_text,
                    "question_image_url": q.question_image_url,
                    "options": options_payload,
                    "time_limit": q.time_limit or quiz.time_per_question or 5
                })

            total_questions = quiz.questions_per_quiz or min(10, len(question_pool))
            time_per_question = quiz.time_per_question or 5
            payload = {
                "type": "quiz_sync",
                "quiz_id": quiz_id,
                "questions_per_quiz": min(total_questions, len(question_pool)),
                "question_pool_size": quiz.question_pool_size or len(question_pool),
                "time_per_question": time_per_question,
                "duration_seconds": min(total_questions, len(question_pool)) * time_per_question,
                "question_pool": question_pool
            }
            await ws_manager.broadcast_to_quiz(quiz_id, payload)

            # Wait for the quiz duration
            await asyncio.sleep(payload["duration_seconds"] + 3)

            # 4. Calculate results
            await self.process_results(quiz_id)

            # 5. Update status to COMPLETED
            quiz.status = "COMPLETED"
            db.commit()
            logger.info(f"Quiz session finished for quiz_id={quiz_id}")
        except Exception as e:
            logger.error(f"Error running quiz session {quiz_id}: {e}")
        finally:
            self.active_quizzes.remove(quiz_id)
            db.close()

    async def process_results(self, quiz_id: int):
        db = SyncSessionLocal()
        try:
            quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
            if not quiz: return

            from models.quiz import QuizResponse
            from sqlalchemy import func

            # 1. Calculate scores for all participants
            # Group by user_id, count is_correct=True, sum response_time_ms
            results = (
                db.query(
                    QuizResponse.user_id,
                    func.count(QuizResponse.id).filter(QuizResponse.is_correct == True).label("score"),
                    func.sum(QuizResponse.response_time_ms).label("total_time")
                )
                .filter(QuizResponse.quiz_id == quiz_id)
                .group_by(QuizResponse.user_id)
                .order_by(func.count(QuizResponse.id).filter(QuizResponse.is_correct == True).desc(), func.sum(QuizResponse.response_time_ms).asc())
                .all()
            )

            if not results:
                logger.warning(f"No responses recorded for quiz_id={quiz_id}")
                return

            # 2. Determine winners and distribute prizes
            # For now, let's give the full pool to the top scorer. 
            # In future, use quiz.prize_distribution JSON.
            
            top_winner_id, top_score, top_time = results[0]
            
            # If multiple people have the same score and time (unlikely), they share.
            winners = [r for r in results if r.score == top_score and r.total_time == top_time]
            prize_per_winner = to_money(quiz.prize_pool) / Decimal(len(winners))

            for w_id, score, time in winners:
                user = db.query(User).filter(User.id == w_id).first()
                if user:
                    credit_wallet(user, prize_per_winner, WALLET_BUCKET_WINNING)
                    
                    tx = WalletTransaction(
                        user_id=user.id,
                        amount=prize_per_winner,
                        transaction_type="QUIZ_WIN",
                        status="SUCCESS",
                        reference_id=f"WIN-QZ-{quiz_id}-{user.id}"
                    )
                    db.add(tx)
                    
                    add_user_notification(
                        db, user.id,
                        "🏆 CHAMPION! 🏆",
                        f"You won ₹{prize_per_winner} in '{quiz.title}'! Score: {score} Correct.",
                        "APP"
                    )
            
            db.commit()
            
            # 3. Notify the room
            winner_names = []
            for w_id, _, _ in winners:
                u = db.query(User).filter(User.id == w_id).first()
                if u: winner_names.append(u.username or f"User {u.id}")
            
            msg = f"Quiz Finished! Winner(s): {', '.join(winner_names)} with {top_score} correct answers!"
            await ws_manager.broadcast_to_quiz(quiz_id, {
                "type": "quiz_result", 
                "message": msg,
                "winners": winner_names,
                "score": int(top_score)
            })

        except Exception as e:
            logger.error(f"Error processing results for quiz {quiz_id}: {e}")
        finally:
            db.close()

orchestrator = QuizOrchestrator()
