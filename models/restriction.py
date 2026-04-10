from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func

from core.database import Base


class UserRestriction(Base):
    __tablename__ = "user_restrictions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # FULL_APP or PAGE
    scope = Column(String(20), nullable=False, index=True)
    # Used when scope=PAGE, for example WALLET or TOURNAMENTS
    page_key = Column(String(64), nullable=True, index=True)

    reason = Column(String(300), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False, index=True)

    created_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    lifted_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    lift_note = Column(String(300), nullable=True)
    lifted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
