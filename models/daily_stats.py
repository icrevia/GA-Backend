from sqlalchemy import Column, Integer, Date, Numeric, DateTime
from sqlalchemy.sql import func
from core.database import Base

class DailyStatsHistory(Base):
    __tablename__ = "daily_stats_history"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, index=True, nullable=False)
    
    total_deposits = Column(Numeric(15, 2), default=0.00)
    total_withdrawals = Column(Numeric(15, 2), default=0.00)
    ff_joining_fees = Column(Numeric(15, 2), default=0.00)
    quiz_joining_fees = Column(Numeric(15, 2), default=0.00)
    ff_prize_distributed = Column(Numeric(15, 2), default=0.00)
    quiz_prize_distributed = Column(Numeric(15, 2), default=0.00)
    spin_distributed = Column(Numeric(15, 2), default=0.00)
    scratch_distributed = Column(Numeric(15, 2), default=0.00)
    free_deposit_given = Column(Numeric(15, 2), default=0.00)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
