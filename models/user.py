from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, ForeignKey
from sqlalchemy.sql import func
from core.database import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    firebase_uid    = Column(String, unique=True, index=True, nullable=True)
    username        = Column(String, unique=True, index=True, nullable=False)
    email           = Column(String, unique=True, index=True, nullable=False)
    phone_number    = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(String, nullable=True)
    role            = Column(String, default="USER")   # USER or ADMIN
    upi_id          = Column(String, nullable=True)
    profile_pic     = Column(String, nullable=True)

    # Path to stored face image for 2FA face verification
    face_image_path = Column(String, nullable=True)

    # Game IDs
    bgmi_id         = Column(String, nullable=True)
    valorant_id     = Column(String, nullable=True)
    freefire_id     = Column(String, nullable=True)

    # Numeric(12,2): exact decimal arithmetic — no floating-point rounding errors
    wallet_balance  = Column(Numeric(precision=12, scale=2), default=0.00)

    is_active       = Column(Boolean, default=True)
    referral_code   = Column(String, unique=True, index=True, nullable=True)
    referred_by_id  = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Token versioning for instant JWT revocation.
    # Increment this (user.token_version += 1) to immediately invalidate
    # all existing tokens for a user (e.g. when banning or force-logout).
    # Added at startup via IF NOT EXISTS migration in main.py.
    token_version   = Column(Integer, default=0, nullable=False, server_default="0")

    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())
