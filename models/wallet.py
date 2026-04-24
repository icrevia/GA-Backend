from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Index
from sqlalchemy.sql import func
from core.database import Base


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        Index("ix_wallet_transactions_user_created", "user_id", "created_at"),
        Index("ix_wallet_transactions_type_status_created", "transaction_type", "status", "created_at"),
    )

    id               = Column(Integer, primary_key=True, index=True)
    user_id          = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # FIXED: Numeric(12,2) instead of Float — exact decimal arithmetic for money
    amount           = Column(Numeric(precision=12, scale=2), nullable=False)

    transaction_type = Column(String, nullable=False, index=True)  # ADD_MONEY, JOIN_TOURNAMENT, PRIZE_WIN, WITHDRAWAL
    status           = Column(String, default="PENDING", index=True)  # PENDING, SUCCESS, FAILED
    reference_id     = Column(String, unique=True, nullable=True)

    # PayU traceability fields
    payu_txn_id      = Column(String, nullable=True, index=True)
    payment_mode     = Column(String, nullable=True)
    failure_reason   = Column(String, nullable=True)

    # Gateway binding fields (used for Razorpay order/payment integrity checks)
    gateway_order_id   = Column(String, nullable=True, index=True)
    gateway_payment_id = Column(String, nullable=True, index=True)
    gateway_signature  = Column(String, nullable=True)
    remark             = Column(String, nullable=True) # Custom display name (e.g. Tournament Title)

    created_at       = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())
