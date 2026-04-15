from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from core.database import Base


class UserActivityLock(Base):
    __tablename__ = "user_activity_locks"
    __table_args__ = (
        UniqueConstraint("user_id", "activity_type", name="uq_user_activity_lock_user_activity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    activity_type = Column(String(40), nullable=False, index=True)

    cycle_key = Column(String(16), nullable=True)
    daily_count = Column(Integer, nullable=False, default=0, server_default="0")
    failed_streak = Column(Integer, nullable=False, default=0, server_default="0")

    is_locked = Column(Boolean, nullable=False, default=False, index=True)
    lock_status = Column(String(60), nullable=True)
    lock_reason = Column(String(300), nullable=True)
    locked_at = Column(DateTime, nullable=True)
    lock_expires_at = Column(DateTime, nullable=True)

    last_attempt_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)

    unlocked_at = Column(DateTime, nullable=True)
    unlocked_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reset_note = Column(String(300), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
