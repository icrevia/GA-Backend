from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.sql import func

from core.database import Base


class OtpPhoneLock(Base):
    __tablename__ = "otp_phone_locks"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    otp_send_count = Column(Integer, nullable=False, default=0, server_default="0")
    is_locked = Column(Boolean, nullable=False, default=False, index=True)
    lock_reason = Column(String(300), nullable=True)
    last_source = Column(String(40), nullable=True)

    first_sent_at = Column(DateTime, nullable=True)
    last_sent_at = Column(DateTime, nullable=True)
    locked_at = Column(DateTime, nullable=True)
    unlocked_at = Column(DateTime, nullable=True)

    unlocked_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reset_note = Column(String(300), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
