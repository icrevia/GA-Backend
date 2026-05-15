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
            # ONLY pick UPCOMING quizzes about to start — never LIVE ones.
            # LIVE quizzes are already handled by run_quiz_session (started by matchmaker).
            # Picking LIVE ones here was causing double sessions.
            to_process = db.query(QuizMatch).filter(
                (QuizMatch.status == "UPCOMING") & 
                (QuizMatch.start_time <= now + timedelta(seconds=60))
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
            
            import random
            final_pool = list(question_pool)
            random.shuffle(final_pool)
            final_pool = final_pool[:questions_per_quiz]

            # Minimum duration is questions * time + buffer
            session_duration = max(60, (len(final_pool) * time_per_question) + 30)
            
            # Save the duration to the quiz object so the REST API also knows it
            quiz.duration_seconds = session_duration
            db.add(quiz)
            db.commit()

            # ── CRITICAL: Release DB connection BEFORE entering the sync loop ─
            # Each match holds the loop for up to 110 seconds. Keeping the DB
            # connection open exhausts the pool (max 15) when concurrent matches run.
            match_type_snapshot = quiz.match_type
            start_time_dt = quiz.start_time
            if start_time_dt.tzinfo is None:
                start_time_dt = start_time_dt.replace(tzinfo=timezone.utc)
            db.close()
            db = None  # Prevent the finally block from double-closing

            end_time = start_time_dt + timedelta(seconds=session_duration)

            # Static payload — questions don't change mid-match
            sync_payload = {
                "type": "quiz_sync",
                "quiz_id": quiz_id,
                "questions_per_quiz": len(final_pool),
                "question_pool_size": len(final_pool),
                "time_per_question": time_per_question,
                "duration_seconds": session_duration,
                "question_pool": final_pool
            }

            # 3. Enter real-time sync loop
            # This loop sends updates every second to keep all players perfectly synced
            logger.info(f"Quiz {quiz_id} sync loop started. Duration: {session_duration}s")
            
            while True:
                now_utc = datetime.now(timezone.utc)
                if now_utc >= end_time:
                    break
                
                # Check if quiz was completed early (e.g. BATTLE finished by all players)
                # We do this every 2 seconds to save DB calls, or use a cached status if available
                # For now, let's just check every second since it's critical for UX
                db_check = SyncSessionLocal()
                try:
                    q_status = db_check.query(QuizMatch.status).filter(QuizMatch.id == quiz_id).scalar()
                    if q_status == "COMPLETED":
                        logger.info(f"Quiz {quiz_id} completed early. Breaking sync loop.")
                        break
                finally:
                    db_check.close()

                # Compute elapsed so Android timer moves correctly every second
                elapsed_secs = int((now_utc - start_time_dt).total_seconds())
                sync_payload["elapsed_seconds"] = elapsed_secs
                await ws_manager.broadcast_to_quiz(quiz_id, sync_payload)
                await asyncio.sleep(1)
            
            # 4. Calculate results
            logger.info(f"Quiz {quiz_id} time up. Processing results...")
            if match_type_snapshot == "BATTLE":
                await self.process_battle_results(quiz_id, force=True)
            else:
                await self.process_results(quiz_id)

            logger.info(f"Quiz session finished for quiz_id={quiz_id}")
        except Exception as e:
            logger.error(f"Error running quiz session {quiz_id}: {e}", exc_info=True)
        finally:
            self.active_quizzes.discard(quiz_id)  # discard is safe even if not in set
            if db is not None:
                db.close()

    async def process_battle_results(self, quiz_id: int, surrendered_user_id: int | None = None, force: bool = False):
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
            
            # CRITICAL: Only proceed if everyone is finished, OR if it's a forced completion (timer/surrender)
            if not force and not surrendered_user_id:
                if not all(p.status == "COMPLETED" for p in participants):
                    logger.info(f"Battle {quiz_id}: Waiting for all participants to complete.")
                    return
            
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
            if surrendered_user_id:
                # If someone surrendered, the OTHER one wins
                winner_id = u2_id if surrendered_user_id == u1_id else u1_id
            else:
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

            # Mark Completed and store stats for leaderboard
            quiz.status = "COMPLETED"
            for p in participants: 
                p.status = "COMPLETED"
                p.score = user_data.get(p.user_id, {}).get("score", 0)
                p.total_time_taken = user_data.get(p.user_id, {}).get("time", 0)
                p.rank = 1 if p.user_id == winner_id else (2 if winner_id else 1) # 1 if win, 2 if loss, 1 if draw
            db.commit()

            # Notify Both with custom payload
            status_u1 = "WON" if winner_id == u1_id else ("DRAW" if not winner_id else "LOST")
            status_u2 = "WON" if winner_id == u2_id else ("DRAW" if not winner_id else "LOST")

            await ws_manager.send_personal_message({
                "type": "quiz_result",
                "quiz_id": quiz_id,
                "status": status_u1,
                "user_score": s1,
                "opponent_score": s2,
                "user_time_ms": t1,
                "opponent_time_ms": t2,
                "winnings": float(prize_pool if winner_id == u1_id else (to_money(quiz.entry_fee) if not winner_id else 0)),
                "surrendered_user_id": surrendered_user_id
            }, u1_id)

            await ws_manager.send_personal_message({
                "type": "quiz_result",
                "quiz_id": quiz_id,
                "status": status_u2,
                "user_score": s2,
                "opponent_score": s1,
                "user_time_ms": t2,
                "opponent_time_ms": t1,
                "winnings": float(prize_pool if winner_id == u2_id else (to_money(quiz.entry_fee) if not winner_id else 0)),
                "surrendered_user_id": surrendered_user_id
            }, u2_id)

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

            # 2. Determine winners and distribute prizes based on prize_distribution
            prize_dist = quiz.prize_distribution or [{"rank": "1", "prize": float(quiz.prize_pool)}]
            
            # results is ordered by score DESC, total_time ASC
            # Assign ranks (handling ties)
            ranked_results = []
            curr_rank = 0
            prev_score = -1
            prev_time = -1
            
            for i, (u_id, score, t_time) in enumerate(results):
                if score != prev_score or t_time != prev_time:
                    curr_rank = i + 1
                ranked_results.append({
                    "user_id": u_id,
                    "score": score,
                    "time": t_time,
                    "rank": curr_rank
                })
                prev_score = score
                prev_time = t_time

            def parse_rank_range(rank_str):
                try:
                    if '-' in str(rank_str):
                        start, end = str(rank_str).split('-')
                        return range(int(start), int(end) + 1)
                    return [int(rank_str)]
                except: return []

            distributed_users = []
            for dist in prize_dist:
                target_ranks = parse_rank_range(dist.get("rank", "0"))
                prize_amount = to_money(dist.get("prize", 0))
                
                if prize_amount <= 0: continue
                
                for res in ranked_results:
                    if res["rank"] in target_ranks:
                        user = db.query(User).filter(User.id == res["user_id"]).first()
                        if user:
                            credit_wallet(user, prize_amount, WALLET_BUCKET_WINNING)
                            db.add(WalletTransaction(
                                user_id=user.id, amount=prize_amount, transaction_type="QUIZ_WIN",
                                status="SUCCESS", reference_id=f"WIN-QZ-{quiz_id}-{user.id}"
                            ))
                            distributed_users.append(user.username or f"User {user.id}")
                            
                            add_user_notification(
                                db, user.id, "🏆 CHAMPION! 🏆",
                                f"You won ₹{prize_amount} in '{quiz.title}'!", "APP"
                            )
            
            # 3. Update Participant table for leaderboard
            for res in ranked_results:
                p = db.query(QuizParticipant).filter(
                    QuizParticipant.quiz_id == quiz_id,
                    QuizParticipant.user_id == res["user_id"]
                ).first()
                if p:
                    p.status = "COMPLETED"
                    p.score = res["score"]
                    p.total_time_taken = res["time"]
                    p.rank = res["rank"]

            quiz.status = "COMPLETED"
            db.commit()
            
            # 3. Notify the room
            msg = f"Quiz Finished! Distributed prizes to {len(distributed_users)} winner(s)!"
            await ws_manager.broadcast_to_quiz(quiz_id, {
                "type": "quiz_result", 
                "message": msg,
                "winners": distributed_users[:5], # Only show top 5 names
                "is_tournament": True
            })
            
            # Refresh lobby for everyone
            await ws_manager.broadcast({"type": "lobby_refresh"})

        except Exception as e:
            logger.error(f"Error processing results for quiz {quiz_id}: {e}")
        finally:
            db.close()

orchestrator = QuizOrchestrator()
