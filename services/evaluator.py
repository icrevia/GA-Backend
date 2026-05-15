import logging
from sqlalchemy.orm import Session
from datetime import datetime
from models.quiz import QuizMatch, QuizParticipant
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import get_total_balance, to_money
import uuid

logger = logging.getLogger("GamerzAdda.evaluator")

def evaluate_survivor_matches(db: Session):
    """
    Finds all SURVIVOR matches that have ended and computes rankings.
    Distributes prizes to winners.
    """
    now = datetime.now()
    
    # 1. Find PENDING survivor matches that have passed their end_time
    matches = (
        db.query(QuizMatch)
        .filter(QuizMatch.match_type == "SURVIVOR")
        .filter(QuizMatch.end_time <= now)
        .filter(QuizMatch.evaluation_status == "PENDING")
        .all()
    )
    
    if not matches:
        return 0

    evaluated_count = 0
    for match in matches:
        try:
            evaluate_single_match(db, match)
            evaluated_count += 1
        except Exception as e:
            logger.error(f"Failed to evaluate match {match.id}: {str(e)}")
            db.rollback()
            
    return evaluated_count

def evaluate_single_match(db: Session, match: QuizMatch):
    logger.info(f"Evaluating Match {match.id}: {match.title}")
    
    # 1. Get all participants
    participants = (
        db.query(QuizParticipant)
        .filter(QuizParticipant.quiz_id == match.id)
        .order_by(QuizParticipant.score.desc(), QuizParticipant.total_time_taken.asc())
        .all()
    )
    
    if not participants:
        match.evaluation_status = "COMPLETED"
        match.status = "COMPLETED"
        db.commit()
        return

    # 2. Assign Ranks
    for i, p in enumerate(participants):
        p.rank = i + 1
        p.status = "COMPLETED" # Ensure status is marked as completed

    # 3. Distribute Prizes based on prize_distribution
    # prize_distribution format: [{"rank": 1, "prize": 500}, {"rank": 2, "prize": 300}]
    prize_map = {}
    if match.prize_distribution:
        for dist in match.prize_distribution:
            rank = dist.get("rank")
            prize = dist.get("prize")
            if rank and prize:
                prize_map[int(rank)] = float(prize)

    for i, p in enumerate(participants):
        rank = i + 1
        prize_amount = prize_map.get(rank, 0)
        
        if prize_amount > 0:
            credit_winning(db, p.user_id, prize_amount, match.id, rank)
            logger.info(f"User {p.user_id} won ₹{prize_amount} (Rank {rank}) in Match {match.id}")

    # 4. Finalize Match
    match.evaluation_status = "COMPLETED"
    match.status = "COMPLETED"
    db.commit()
    logger.info(f"Match {match.id} evaluation completed.")

def credit_winning(db: Session, user_id: int, amount: float, match_id: int, rank: int):
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        return

    money_amount = to_money(amount)
    user.wallet_winning += money_amount
    
    transaction = WalletTransaction(
        user_id=user_id,
        amount=money_amount,
        transaction_type="QUIZ_WINNING",
        status="SUCCESS",
        reference_id=f"WIN-{uuid.uuid4().hex[:6].upper()}",
        failure_reason=f"MATCH:{match_id}|RANK:{rank}"
    )
    db.add(transaction)
    # We don't commit here, the caller (evaluate_single_match) will commit.
