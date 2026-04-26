from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from decimal import Decimal
import uuid

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
        raise HTTPException(status_code=400, detail="Quiz has already started or completed")
    
    existing = db.query(QuizParticipant).filter(
        QuizParticipant.quiz_id == quiz_id,
        QuizParticipant.user_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already joined this quiz")

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
