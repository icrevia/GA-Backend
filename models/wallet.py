from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func
from core.database import Base

class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False) # Positive for credit, negative for debit
    transaction_type = Column(String, nullable=False) # ADD_MONEY, JOIN_TOURNAMENT, PRIZE_WIN, WITHDRAWAL
    status = Column(String, default="PENDING") # PENDING, SUCCESS, FAILED
    reference_id = Column(String, unique=True, nullable=True) # Our internal ZEX_xxx txnid
    
    # PayU fields for full traceability
    payu_txn_id = Column(String, nullable=True, index=True) # PayU's own mihpayid
    payment_mode = Column(String, nullable=True)  # UPI / CC / DC / NB / WALLET
    failure_reason = Column(String, nullable=True) # USERCANCELLED / bank error message
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
