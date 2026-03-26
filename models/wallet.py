from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey
from sqlalchemy.sql import func
from core.database import Base


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # FIXED: Numeric(12,2) instead of Float — exact decimal arithmetic for money
    amount           = Column(Numeric(precision=12, scale=2), nullable=False)

    transaction_type = Column(String, nullable=False)  # ADD_MONEY, JOIN_TOURNAMENT, PRIZE_WIN, WITHDRAWAL
    status           = Column(String, default="PENDING")  # PENDING, SUCCESS, FAILED
    reference_id     = Column(String, unique=True, nullable=True)

    # PayU traceability fields
    payu_txn_id      = Column(String, nullable=True, index=True)
    payment_mode     = Column(String, nullable=True)
    failure_reason   = Column(String, nullable=True)

    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())
