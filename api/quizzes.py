from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Dict, Any
from decimal import Decimal
from datetime import datetime, timedelta
import random
import uuid
import logging

logger = logging.getLogger("GamerzAdda.quizzes")

from api.deps import get_current_user_quizzes as get_current_user
from core.database import get_db_sync as get_db
from models.user import User
from models.quiz import QuizMatch, QuizQuestion, QuizParticipant
from models.wallet import WalletTransaction
from schemas.quiz import QuizMatchResponse, QuizJoinResponse, QuizSubmissionRequest, QuizSubmissionResponse
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    InsufficientWalletBalanceError,
    debit_wallet,
    get_total_balance,
    to_money,
)

router = APIRouter()

@router.get("/upcoming", response_model=List[QuizMatchResponse])
def get_upcoming_quizzes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Join count subquery
    joined_subq = (
        db.query(
            QuizParticipant.quiz_id,
            func.count(QuizParticipant.id).label('j_count')
        )
        .group_by(QuizParticipant.quiz_id)
        .subquery()
    )

    user_participation = {
        qid: status for (qid, status) in db.query(QuizParticipant.quiz_id, QuizParticipant.status)
        .filter(QuizParticipant.user_id == current_user.id)
        .all()
    }

    rows = (
        db.query(QuizMatch, func.coalesce(joined_subq.c.j_count, 0))
        .outerjoin(joined_subq, QuizMatch.id == joined_subq.c.quiz_id)
        .filter(
            (QuizMatch.status.in_(["UPCOMING", "LIVE"])) |
            ((QuizMatch.status == "COMPLETED") & (QuizMatch.id.in_(list(user_participation.keys()))))
        )
        .order_by(QuizMatch.start_time.desc())
        .all()
    )

    result = []
    for q, count in rows:
        q.joined_count = count
        q.is_joined = q.id in user_participation
        q.is_played = user_participation.get(q.id) == "COMPLETED"
        result.append(q)
    
    return result

@router.get("/{quiz_id}/questions", response_model=dict)
def get_quiz_questions(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    REST Backup: Returns the question pool for a LIVE quiz. 
    Used by the app if WebSocket fails to deliver the 'quiz_sync' payload.
    """
    quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).first()
    
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    if quiz.status != "LIVE":
        raise HTTPException(status_code=400, detail=f"Quiz is not LIVE (status={quiz.status})")

    # Verify participation
    participant = db.query(QuizParticipant).filter(
        QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.user_id == current_user.id
    ).first()
    
    if not participant:
        raise HTTPException(status_code=403, detail="You have not joined this quiz")
    
    if participant.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="You have already completed this match.")

    questions = db.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.id.asc()).all()
    if quiz.question_pool_size:
        questions = questions[:quiz.question_pool_size]
    
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this quiz")

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
            "time_limit": q.time_limit or quiz.time_per_question or 5,
        })

    # Determine actual counts and timers from Admin Settings
    questions_per_quiz = quiz.questions_per_quiz if (quiz.questions_per_quiz and quiz.questions_per_quiz > 0) else 10
    
    if quiz.match_type == "SURVIVOR":
        # Randomize subset for survivor mode using a user-specific seed
        rng = random.Random(current_user.id + quiz_id)
        final_pool = rng.sample(question_pool, min(len(question_pool), questions_per_quiz))
        
        # Shuffle options within each question using a deterministic seed for that specific question
        for q in final_pool:
            seed = f"shuff:{current_user.id}:{quiz_id}:{q['id']}"
            q_rng = random.Random(seed)
            
            # Use index-based shuffling to match submitQuizAnswer logic exactly
            indices = list(range(len(q["options"])))
            q_rng.shuffle(indices)
            
            # Reorder options based on shuffled indices
            original_options = list(q["options"])
            q["options"] = [original_options[i] for i in indices]
            
            logger.info(f"FETCH: User {current_user.id}, Quiz {quiz_id}, Question {q['id']}, Seed {seed}, Order {indices}")
    else:
        # Limit questions to the requested amount (don't shuffle here to keep it stable per user request)
        final_pool = question_pool[:questions_per_quiz]
    
    # Use stored duration if available, else calculate
    tpq = quiz.time_per_question or 10
    session_duration = quiz.duration_seconds or max(60, (len(final_pool) * tpq) + 30)
    
    elapsed_seconds = 0
    if participant.user_start_time:
        elapsed_seconds = int((datetime.now() - participant.user_start_time).total_seconds())

    return {
        "quiz_id": quiz_id,
        "questions_per_quiz": len(final_pool),
        "question_pool_size": len(final_pool),
        "time_per_question": tpq,
        "duration_seconds": session_duration,
        "elapsed_seconds": elapsed_seconds,
        "question_pool": final_pool,
    }

@router.post("/{quiz_id}/join", response_model=QuizJoinResponse)
def join_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    quiz = db.query(QuizMatch).filter(QuizMatch.id == quiz_id).with_for_update().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    if quiz.match_type == "SURVIVOR":
        if quiz.end_time and datetime.now() > (quiz.end_time - timedelta(minutes=5)):
            raise HTTPException(status_code=400, detail="Result Preparing: No more joins allowed.")
    elif quiz.status != "UPCOMING":
        logger.warning(f"Join failed: quiz {quiz_id} status is {quiz.status}")
        raise HTTPException(status_code=400, detail="Quiz has already started or completed")
    
    existing = db.query(QuizParticipant).filter(
        QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.user_id == current_user.id
    ).first()
    if existing:
        logger.warning(f"Join failed: user {current_user.id} already joined quiz {quiz_id}")
        raise HTTPException(status_code=400, detail="Already joined this quiz")

    current_count = db.query(QuizParticipant).filter(QuizParticipant.quiz_id == quiz_id).count()
    if quiz.max_participants and current_count >= quiz.max_participants:
        logger.warning(f"Join failed: quiz {quiz_id} is full ({current_count}/{quiz.max_participants})")
        raise HTTPException(status_code=400, detail="Quiz is full")

    total_fee = to_money(quiz.entry_fee)
    user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    
    try:
        # Check total balance
        if get_total_balance(user_wallet) < total_fee:
             raise InsufficientWalletBalanceError(required=total_fee, available=get_total_balance(user_wallet))
        
        # Deduct from winning -> deposit -> bonus (default order)
        deductions = debit_wallet(user_wallet, total_fee, spend_order=[WALLET_BUCKET_WINNING, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_BONUS])
        
        participant = QuizParticipant(
            quiz_id=quiz_id,
            user_id=current_user.id,
            user_start_time=datetime.now()
        )
        db.add(participant)
        
        transaction = WalletTransaction(
            user_id=current_user.id,
            amount=-total_fee,
            transaction_type="JOIN_QUIZ",
            status="SUCCESS",
            reference_id=f"QZ-{uuid.uuid4().hex[:6].upper()}",
            failure_reason=f"QUIZ:{quiz_id}"
        )
        db.add(transaction)
        
        db.commit()
        db.refresh(user_wallet)
        
        return QuizJoinResponse(
            message="Successfully joined the quiz!",
            new_wallet_balance=float(get_total_balance(user_wallet)),
            quiz_id=quiz_id,
            deduction_breakdown={k: float(v) for k, v in deductions.items()}
        )
        
    except InsufficientWalletBalanceError as exc:
        db.rollback()
        logger.warning(f"Join failed: user {current_user.id} insufficient balance. Required={exc.required}, Available={exc.available}")
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Insufficient balance! Required: ₹{exc.required:.2f}, Available: ₹{exc.available:.2f}",
                "error_code": "INSUFFICIENT_BALANCE",
                "required": float(exc.required),
                "available": float(exc.available)
            }
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/online-count")
def get_online_count():
    from core.websockets import manager
    import random
    # Base users + active connections + random jitter for a 'Live' feel
    actual_connections = len(manager.active_connections)
    jitter = random.randint(-5, 8) 
    return {"count": 1240 + actual_connections + jitter}

@router.get("/{quiz_id}/leaderboard")
def get_quiz_leaderboard(
    quiz_id: int,
    db: Session = Depends(get_db)
):
    """
    Returns the leaderboard for a specific quiz match.
    """
    participants = (
        db.query(QuizParticipant)
        .filter(QuizParticipant.quiz_id == quiz_id)
        .order_by(QuizParticipant.rank.asc())
        .all()
    )
    
    leaderboard = []
    for p in participants:
        leaderboard.append({
            "user_id": p.user_id,
            "username": p.user.username,
            "profile_pic": p.user.profile_pic,
            "score": p.score,
            "total_time_taken": float(p.total_time_taken),
            "rank": p.rank
        })
        
    return leaderboard

@router.post("/submit-answer", response_model=QuizSubmissionResponse)
def submit_answer(
    req: QuizSubmissionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    REST Submission for Survival Mode (Async). 
    Records response time and score server-side.
    """
    participant = db.query(QuizParticipant).filter(
        QuizParticipant.quiz_id == req.quiz_id,
        QuizParticipant.user_id == current_user.id
    ).with_for_update().first()
    
    if not participant:
        raise HTTPException(status_code=403, detail="You are not a participant in this quiz")
    
    if participant.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="You have already finished this quiz")

    question = db.query(QuizQuestion).filter(QuizQuestion.id == req.question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    # For SURVIVOR, we need to account for option shuffling done in get_quiz_questions
    quiz = db.query(QuizMatch).filter(QuizMatch.id == req.quiz_id).first()
    correct_idx = question.correct_option_index
    
    if quiz and quiz.match_type == "SURVIVOR":
        # Reconstruct the shuffled options to find the correct index using the question-specific seed
        seed = f"shuff:{current_user.id}:{req.quiz_id}:{req.question_id}"
        q_rng = random.Random(seed)
        
        original_options = list(question.options or [])
        shuffled_indices = [i for i in range(len(original_options))]
        q_rng.shuffle(shuffled_indices)
        
        # The user sent 'req.option_index', which is an index in the SHUFFLED list.
        # So we need to check if shuffled_indices[req.option_index] == original_correct_idx
        is_correct = (shuffled_indices[req.option_index] == correct_idx)
        
        # Correct index in the shuffled list for the response
        correct_option_index_in_shuffled = shuffled_indices.index(correct_idx)
        logger.info(f"SUBMIT: User picked {req.option_index} (orig index {shuffled_indices[req.option_index]}). Correct is {correct_idx} (shuffled index {correct_option_index_in_shuffled})")
    else:
        is_correct = (req.option_index == correct_idx)
        correct_option_index_in_shuffled = correct_idx

    # Check if answer already submitted for this question
    existing = db.query(QuizResponse).filter(
        QuizResponse.quiz_id == req.quiz_id,
        QuizResponse.user_id == current_user.id,
        QuizResponse.question_id == req.question_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Answer already submitted for this question")

    # Timing Logic
    now = datetime.now()
    last_response = db.query(QuizResponse).filter(
        QuizResponse.quiz_id == req.quiz_id,
        QuizResponse.user_id == current_user.id
    ).order_by(QuizResponse.created_at.desc()).first()

    if last_response:
        delta = (now - last_response.created_at).total_seconds() * 1000
    else:
        # First question
        start_time = participant.user_start_time or participant.joined_at
        delta = (now - start_time).total_seconds() * 1000

    score_delta = 10 if is_correct else 0
    
    response = QuizResponse(
        quiz_id=req.quiz_id,
        question_id=req.question_id,
        user_id=current_user.id,
        option_index=req.option_index,
        is_correct=is_correct,
        response_time_ms=int(delta)
    )
    db.add(response)
    
    if is_correct:
        participant.score += score_delta
    participant.total_time_taken += int(delta)
    
    # Mark as COMPLETED if all questions answered
    responses_count = db.query(QuizResponse).filter(
        QuizResponse.quiz_id == req.quiz_id,
        QuizResponse.user_id == current_user.id
    ).count()
    
    if responses_count >= (quiz.questions_per_quiz or 10):
        participant.status = "COMPLETED"
        
    db.commit()
    
    return {
        "success": True,
        "message": "Answer recorded",
        "is_correct": is_correct,
        "correct_option_index": correct_option_index_in_shuffled,
        "score_delta": score_delta
    }
