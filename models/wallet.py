from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum
from sqlalchemy.sql import func
from core.database import Base

class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False) # Positive for credit, negative for debit
    transaction_type = Column(String, nullable=False) # ADD_MONEY, JOIN_TOURNAMENT, PRIZE_WIN, WITHDRAWAL
    status = Column(String, default="PENDING") # PENDING, SUCCESS, FAILED
    reference_id = Column(String, unique=True, nullable=True) # PayU txnid or Tournament ID
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
