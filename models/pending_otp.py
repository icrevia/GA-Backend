from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func
from core.database import Base


class PendingOtp(Base):
    """
    DB-backed store for pending OTP verifications and pending signups.
    Replaces in-memory _otp_store and _pending_signups dicts in auth.py.
    Survives server restarts and is safe under concurrent load.
    """
    __tablename__ = "pending_otps"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, nullable=False, index=True)

    # SMS provider verification ID (from OTP service)
    verification_id = Column(String(256), nullable=True)

    # Pending signup data (JSON stored as text)
    pending_username = Column(String(64), nullable=True)
    pending_email = Column(String(256), nullable=True)
    pending_referral_code = Column(String(64), nullable=True)

    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
