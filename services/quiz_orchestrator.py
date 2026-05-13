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
        # 1. Check questions and timing
        db = SyncSessionLocal()
        try:
            quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
            if not quiz: return

            # 2. Get questions
            questions = db.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.id.asc()).all()
            if quiz.question_pool_size:
                questions = questions[:quiz.question_pool_size]
            
            if not questions:
                # If it's more than 5 minutes past start time and still no questions, expire it.
                now = datetime.now(timezone.utc)
                if quiz.start_time < now - timedelta(minutes=5):
                    logger.warning(f"Quiz {quiz_id} expired (no questions for 5 mins).")
                    quiz.status = "COMPLETED"
                    db.commit()
                    return
                
                logger.warning(f"No questions for quiz_id={quiz_id}, skipping start for now.")
                # We return and don't change status to LIVE. 
                # The next cycle will try again because we'll remove it from active_quizzes in 'finally'.
                return

            logger.info(f"Starting quiz session for quiz_id={quiz_id} with {len(questions)} questions")

            # 3. Wait until actual start_time before going LIVE
            now = datetime.now(timezone.utc)
            start = quiz.start_time
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            wait_secs = (start - now).total_seconds()
            if wait_secs > 0:
                logger.info(f"Quiz {quiz_id}: waiting {wait_secs:.1f}s until scheduled start")
                db.close()
                db = None
                await asyncio.sleep(wait_secs)
                db = SyncSessionLocal()
                quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
                if not quiz or quiz.status == "COMPLETED":
                    return
                # Re-fetch questions in case admin added more
                questions = db.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.id.asc()).all()
                if not questions:
                    logger.warning(f"Quiz {quiz_id}: still no questions at start time, expiring.")
                    quiz.status = "COMPLETED"
                    db.commit()
                    return

            # Mark LIVE
            quiz.status = "LIVE"
            db.add(quiz)
            db.commit()
            
            from core.websockets import manager as ws_manager
            await ws_manager.broadcast({"type": "lobby_refresh"})

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

            # Determine actual counts and timers from Admin Settings
            questions_per_quiz = quiz.questions_per_quiz if (quiz.questions_per_quiz and quiz.questions_per_quiz > 0) else 10
            time_per_question = quiz.time_per_question if (quiz.time_per_question and quiz.time_per_question > 0) else 5
            
            # Limit the questions we actually send to the pool size requested
            # We shuffle here to give everyone the same set but a random subset of the total pool if needed
            import random
            final_pool = question_pool
            random.shuffle(final_pool)
            final_pool = final_pool[:questions_per_quiz]

            # Minimum duration is questions * time + buffer
            session_duration = max(60, (len(final_pool) * time_per_question) + 30)
            
            payload = {
                "type": "quiz_sync",
                "quiz_id": quiz_id,
                "questions_per_quiz": len(final_pool),
                "question_pool_size": len(final_pool),
                "time_per_question": time_per_question,
                "duration_seconds": session_duration,
                "question_pool": final_pool
            }
            
            # Save the duration to the quiz object so the REST API also knows it
            quiz.duration_seconds = session_duration
            db.add(quiz)
            db.commit()

            logger.info(f"Broadcasting quiz_sync for quiz {quiz_id}. Duration: {session_duration}s")
            await ws_manager.broadcast_to_quiz(quiz_id, payload)

            # Wait for the quiz duration + extra grace period
            logger.info(f"Quiz {quiz_id} session sleeping for {session_duration}s")
            await asyncio.sleep(session_duration)

            # 4. Calculate results
            logger.info(f"Quiz {quiz_id} time up. Processing results...")
            await self.process_results(quiz_id)

            # 5. Update status to COMPLETED
            quiz.status = "COMPLETED"
            db.commit()

            # Mark all participants as COMPLETED
            from sqlalchemy import update
            db.execute(
                update(QuizParticipant)
                .where(QuizParticipant.quiz_id == quiz_id)
                .values(status="COMPLETED")
            )
            db.commit()
            
            logger.info(f"Quiz session finished for quiz_id={quiz_id}")
        except Exception as e:
            logger.error(f"Error running quiz session {quiz_id}: {e}")
        finally:
            self.active_quizzes.remove(quiz_id)
            db.close()

    async def process_battle_results(self, quiz_id: int):
        """
        Specialized result calculation for 1v1 Battles. 
        Calculates winner based on Score -> then Response Time.
        Distributes prizes immediately.
        """
        db = SyncSessionLocal()
        try:
            quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
            if not quiz or quiz.match_type != "BATTLE" or quiz.status == "COMPLETED": 
                return

            from models.quiz import QuizResponse, QuizParticipant
            from sqlalchemy import func

            # Get scores and times for both participants
            results = (
                db.query(
                    QuizResponse.user_id,
                    func.count(QuizResponse.id).filter(QuizResponse.is_correct == True).label("score"),
                    func.sum(QuizResponse.response_time_ms).label("total_time")
                )
                .filter(QuizResponse.quiz_id == quiz_id)
                .group_by(QuizResponse.user_id)
                .all()
            )

            participants = db.query(QuizParticipant).filter(QuizParticipant.quiz_id == quiz_id).all()
            if len(participants) < 2: return # Wait for both
            
            # If we don't have results for someone yet (they finished but zero answers? or bot didn't finish?)
            # Actually, this is called when both are supposed to be done.
            
            # Map results
            user_data = {r.user_id: {"score": int(r.score), "time": int(r.total_time or 0)} for r in results}
            
            # Ensure everyone is in the map even if 0 score
            for p in participants:
                if p.user_id not in user_data:
                    user_data[p.user_id] = {"score": 0, "time": 0}

            u1_id = participants[0].user_id
            u2_id = participants[1].user_id
            
            s1, t1 = user_data[u1_id]["score"], user_data[u1_id]["time"]
            s2, t2 = user_data[u2_id]["score"], user_data[u2_id]["time"]

            winner_id = None
            if s1 > s2: winner_id = u1_id
            elif s2 > s1: winner_id = u2_id
            else:
                # TIE BREAKER: Fastest overall response time
                if t1 < t2: winner_id = u1_id
                elif t2 < t1: winner_id = u2_id
                else: winner_id = None # PURE DRAW

            # Distribute Prizes
            prize_pool = to_money(quiz.prize_pool)
            if winner_id:
                winner_user = db.query(User).filter(User.id == winner_id).first()
                if winner_user:
                    credit_wallet(winner_user, prize_pool, WALLET_BUCKET_WINNING)
                    db.add(WalletTransaction(
                        user_id=winner_id, amount=prize_pool, transaction_type="QUIZ_WIN",
                        status="SUCCESS", reference_id=f"BATTLE-WIN-{quiz_id}"
                    ))
            else:
                # DRAW: Return entry fee to both
                draw_refund = to_money(quiz.entry_fee)
                for p in participants:
                    u = db.query(User).filter(User.id == p.user_id).first()
                    if u: credit_wallet(u, draw_refund, WALLET_BUCKET_WINNING)

            # Mark Completed
            quiz.status = "COMPLETED"
            for p in participants: p.status = "COMPLETED"
            db.commit()

            # Notify Both with custom payload
            async def notify_player(uid, status, u_score, o_score, u_time, o_time, winnings):
                await ws_manager.send_personal_message({
                    "type": "quiz_result",
                    "quiz_id": quiz_id,
                    "status": status,
                    "user_score": u_score,
                    "opponent_score": o_score,
                    "user_time_ms": u_time,
                    "opponent_time_ms": o_time,
                    "winnings": float(winnings)
                }, uid)

            await notify_player(u1_id, "WON" if winner_id == u1_id else ("DRAW" if not winner_id else "LOST"), s1, s2, t1, t2, prize_pool if winner_id == u1_id else (to_money(quiz.entry_fee) if not winner_id else 0))
            await notify_player(u2_id, "WON" if winner_id == u2_id else ("DRAW" if not winner_id else "LOST"), s2, s1, t2, t1, prize_pool if winner_id == u2_id else (to_money(quiz.entry_fee) if not winner_id else 0))

        except Exception as e:
            logger.error(f"Error in process_battle_results for quiz {quiz_id}: {e}")
        finally:
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
            
            # Refresh lobby for everyone
            await ws_manager.broadcast({"type": "lobby_refresh"})

        except Exception as e:
            logger.error(f"Error processing results for quiz {quiz_id}: {e}")
        finally:
            db.close()

orchestrator = QuizOrchestrator()
