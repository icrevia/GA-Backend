from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from core.database import Base


class WithdrawUpiAccount(Base):
    __tablename__ = "withdraw_upi_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "upi_id", name="uq_withdraw_upi_accounts_user_upi"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_holder_name = Column(String(120), nullable=False)
    upi_id = Column(String(120), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
