from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger("GamerzAdda.quizzes")

from api.deps import get_current_user_quizzes as get_current_user
from core.database import get_db_sync as get_db
from models.user import User
from models.quiz import QuizMatch, QuizQuestion, QuizParticipant
from models.wallet import WalletTransaction
from schemas.quiz import QuizMatchResponse, QuizJoinResponse
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

    rows = (
        db.query(QuizMatch, func.coalesce(joined_subq.c.j_count, 0))
        .outerjoin(joined_subq, QuizMatch.id == joined_subq.c.quiz_id)
        .filter(QuizMatch.status.in_(["UPCOMING", "LIVE"]))
        .order_by(QuizMatch.start_time.asc())
        .all()
    )

    user_joined_ids = {
        qid for (qid,) in db.query(QuizParticipant.quiz_id)
        .filter(QuizParticipant.user_id == current_user.id)
        .all()
    }

    result = []
    for q, count in rows:
        q.joined_count = count
        q.is_joined = q.id in user_joined_ids
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
    time_per_question = quiz.time_per_question if (quiz.time_per_question and quiz.time_per_question > 0) else 5
    
    # Limit questions to the requested amount (don't shuffle here to keep it stable per user request)
    final_pool = question_pool[:questions_per_quiz]
    
    # Use stored duration if available, else calculate
    session_duration = quiz.duration_seconds or max(60, (len(final_pool) * time_per_question) + 30)

    return {
        "quiz_id": quiz_id,
        "questions_per_quiz": len(final_pool),
        "question_pool_size": len(final_pool),
        "time_per_question": time_per_question,
        "duration_seconds": session_duration,
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
    
    if quiz.status != "UPCOMING":
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
            user_id=current_user.id
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
