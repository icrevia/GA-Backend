import asyncio
import logging
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import func
from core.database import SyncSessionLocal
from models.wallet import WalletTransaction
from models.daily_stats import DailyStatsHistory

logger = logging.getLogger("GamerzAdda.daily_stats")

IST_OFFSET = timedelta(hours=5, minutes=30)
IST_TZ = timezone(IST_OFFSET)

FREE_DEPOSIT_TX_TYPES = [
    "SIGNUP_CREDIT", 
    "SIGNUP_BONUS", 
    "DEPOSIT_BONUS", 
    "REFERRAL_REWARD", 
    "PROMO_REWARD"
]

def get_ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST_TZ)

def generate_daily_snapshot(db: Session, target_date: date) -> DailyStatsHistory:
    """Aggregates stats for the given calendar day in IST."""
    start_dt_ist = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=IST_TZ)
    end_dt_ist = start_dt_ist + timedelta(days=1)
    
    start_dt_utc = start_dt_ist.astimezone(timezone.utc)
    end_dt_utc = end_dt_ist.astimezone(timezone.utc)

    # Helper for sum queries
    def get_sum(tx_type_in, status="SUCCESS"):
        if isinstance(tx_type_in, str):
            tx_type_in = [tx_type_in]
        
        q = db.query(func.sum(WalletTransaction.amount)).filter(
            WalletTransaction.transaction_type.in_(tx_type_in),
            WalletTransaction.created_at >= start_dt_utc,
            WalletTransaction.created_at < end_dt_utc
        )
        if status:
            q = q.filter(WalletTransaction.status == status)
        return q.scalar() or Decimal("0.00")

    total_deposits = get_sum("ADD_MONEY")
    total_withdrawals = get_sum("WITHDRAWAL")
    
    # Calculate net joining fees (Gross entry - Refunds)
    ff_joining_fees = abs(get_sum("JOIN_TOURNAMENT", status=None)) - abs(get_sum("TOURNAMENT_CANCEL_REFUND", status=None))
    quiz_joining_fees = abs(get_sum("QUIZ_ENTRY", status=None)) - abs(get_sum("QUIZ_REFUND", status=None))
    
    ff_prize_distributed = get_sum("PRIZE_WIN", status=None)
    quiz_prize_distributed = get_sum("QUIZ_WIN", status=None)
    spin_distributed = get_sum("SPIN_REWARD", status=None)
    scratch_distributed = get_sum("SCRATCH_CARD_REWARD", status=None)
    free_deposit_given = get_sum(FREE_DEPOSIT_TX_TYPES, status=None)

    # Check if record already exists
    record = db.query(DailyStatsHistory).filter(DailyStatsHistory.date == target_date).first()
    if not record:
        record = DailyStatsHistory(date=target_date)
        db.add(record)

    record.total_deposits = total_deposits
    record.total_withdrawals = total_withdrawals
    record.ff_joining_fees = ff_joining_fees
    record.quiz_joining_fees = quiz_joining_fees
    record.ff_prize_distributed = ff_prize_distributed
    record.quiz_prize_distributed = quiz_prize_distributed
    record.spin_distributed = spin_distributed
    record.scratch_distributed = scratch_distributed
    record.free_deposit_given = free_deposit_given

    db.commit()
    return record

def retention_cleanup(db: Session):
    """Deletes records older than 6 months, or 1 month if count > 180."""
    try:
        total_records = db.query(func.count(DailyStatsHistory.id)).scalar() or 0
        
        retention_days = 180 # 6 months
        if total_records > 180:
            retention_days = 30 # reduce to 1 month
        
        cutoff_date = get_ist_now().date() - timedelta(days=retention_days)
        
        deleted = db.query(DailyStatsHistory).filter(DailyStatsHistory.date < cutoff_date).delete()
        if deleted > 0:
            db.commit()
            logger.info(f"Cleanup deleted {deleted} daily stat records older than {cutoff_date}")
    except Exception as e:
        logger.error(f"Error during retention cleanup: {e}")
        db.rollback()

async def daily_stats_scheduler():
    """Runs periodically to aggregate stats for the previous day."""
    await asyncio.sleep(60) # initial delay
    
    while True:
        try:
            now_ist = get_ist_now()
            # We want to aggregate data for yesterday
            yesterday = now_ist.date() - timedelta(days=1)
            
            # Check if yesterday's stats are already generated
            db = SyncSessionLocal()
            try:
                record = db.query(DailyStatsHistory).filter(DailyStatsHistory.date == yesterday).first()
                if not record:
                    # Not generated yet. Let's do it if it's past midnight.
                    # It's always past midnight for yesterday, but let's just make sure it runs.
                    generate_daily_snapshot(db, yesterday)
                    logger.info(f"Daily stats snapshot generated for {yesterday}")
                    
                    # Run cleanup too
                    retention_cleanup(db)
            finally:
                db.close()
                
                
        except Exception as e:
            logger.error(f"Error in daily stats scheduler: {e}")
            await asyncio.sleep(60)
            continue
            
        # Check every hour
        await asyncio.sleep(3600)
