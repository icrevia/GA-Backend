import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime
from models.quiz import QuizMatch, QuizParticipant
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import WALLET_BUCKET_WINNING, credit_wallet, to_money
import uuid

logger = logging.getLogger("GamerzAdda.evaluator")

async def evaluate_survivor_matches(db: AsyncSession):
    """
    Finds all SURVIVOR matches that have ended and computes rankings.
    Distributes prizes to winners.
    """
    now = datetime.now()
    
    # 1. Find PENDING survivor matches that have passed their end_time
    stmt = select(QuizMatch).filter(
        QuizMatch.match_type == "SURVIVOR",
        QuizMatch.end_time <= now,
        QuizMatch.evaluation_status == "PENDING"
    )
    result = await db.execute(stmt)
    matches = result.scalars().all()
    
    if not matches:
        return 0

    evaluated_count = 0
    for match in matches:
        try:
            await evaluate_single_match(db, match)
            evaluated_count += 1
        except Exception as e:
            logger.error(f"Failed to evaluate match {match.id}: {str(e)}")
            await db.rollback()
            
    return evaluated_count

async def evaluate_single_match(db: AsyncSession, match: QuizMatch):
    logger.info(f"Evaluating Match {match.id}: {match.title}")
    
    # 1. Get all participants
    stmt = select(QuizParticipant).filter(
        QuizParticipant.quiz_id == match.id
    ).order_by(
        QuizParticipant.score.desc(), 
        QuizParticipant.total_time_taken.asc()
    )
    result = await db.execute(stmt)
    participants = result.scalars().all()
    
    if not participants:
        match.evaluation_status = "COMPLETED"
        match.status = "COMPLETED"
        await db.commit()
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
            await credit_winning(db, p.user_id, prize_amount, match.id, rank)
            logger.info(f"User {p.user_id} won ₹{prize_amount} (Rank {rank}) in Match {match.id}")

    # 4. Finalize Match
    match.evaluation_status = "COMPLETED"
    match.status = "COMPLETED"
    await db.commit()
    logger.info(f"Match {match.id} evaluation completed.")

async def credit_winning(db: AsyncSession, user_id: int, amount: float, match_id: int, rank: int):
    # Select user with row-level locking
    stmt = select(User).filter(User.id == user_id).with_for_update()
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        return

    money_amount = to_money(amount)
    credit_wallet(user, money_amount, WALLET_BUCKET_WINNING)
    
    transaction = WalletTransaction(
        user_id=user_id,
        amount=money_amount,
        transaction_type="QUIZ_WIN",
        status="SUCCESS",
        reference_id=f"WIN-{uuid.uuid4().hex[:6].upper()}",
        failure_reason=f"MATCH:{match_id}|RANK:{rank}"
    )
    db.add(transaction)
