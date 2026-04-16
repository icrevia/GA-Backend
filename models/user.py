from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String, unique=True, index=True, nullable=False)
    email           = Column(String, unique=True, index=True, nullable=False)
    phone_number    = Column(String, unique=True, index=True, nullable=True)
    role            = Column(String, default="USER")   # USER or ADMIN

    profile_pic     = Column(String, nullable=True)
    bio             = Column(String(30), nullable=True)

    # Relationships
    restrictions = relationship("UserRestriction", primaryjoin="User.id == UserRestriction.user_id", foreign_keys="UserRestriction.user_id")


    # Game IDs
    freefire_id     = Column(String, nullable=True)

    # Numeric(12,2): exact decimal arithmetic — no floating-point rounding errors
    wallet_balance  = Column(Numeric(precision=12, scale=2), default=0.00)
    deposit_balance = Column(Numeric(precision=12, scale=2), nullable=False, default=0.00, server_default="0")
    winning_balance = Column(Numeric(precision=12, scale=2), nullable=False, default=0.00, server_default="0")
    bonus_balance   = Column(Numeric(precision=12, scale=2), nullable=False, default=0.00, server_default="0")

    is_active       = Column(Boolean, default=True)
    referral_code   = Column(String, unique=True, index=True, nullable=True)
    referred_by_id  = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Token versioning for instant JWT revocation.
    # Increment this (user.token_version += 1) to immediately invalidate
    # all existing tokens for a user (e.g. when banning or force-logout).
    # Added at startup via IF NOT EXISTS migration in main.py.
    token_version   = Column(Integer, default=0, nullable=False, server_default="0")
    last_login_ip   = Column(String(64), nullable=True)
    last_login_device = Column(String(160), nullable=True)
    last_login_at   = Column(DateTime, nullable=True)
    daily_spin_limit = Column(Integer, nullable=False, default=1, server_default="1")
    daily_spin_used = Column(Integer, nullable=False, default=0, server_default="0")
    daily_spin_cycle_key = Column(String(16), nullable=True)
    daily_bonus_used = Column(Numeric(precision=12, scale=2), nullable=False, default=0.00, server_default="0")
    daily_bonus_cycle_key = Column(String(16), nullable=True)

    # FCM push notification token (refreshed by Android app automatically)
    fcm_token       = Column(String(512), nullable=True)

    created_at      = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())

