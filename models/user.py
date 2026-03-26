from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric
from sqlalchemy.sql import func
from core.database import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    firebase_uid    = Column(String, unique=True, index=True, nullable=True)
    username        = Column(String, unique=True, index=True, nullable=False)
    email           = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    role            = Column(String, default="USER")   # USER or ADMIN
    upi_id          = Column(String, nullable=True)
    profile_pic     = Column(String, nullable=True)

    # Game IDs
    bgmi_id         = Column(String, nullable=True)
    valorant_id     = Column(String, nullable=True)
    freefire_id     = Column(String, nullable=True)

    # FIXED: Use Numeric(10,2) instead of Float — IEEE 754 float is not safe for money
    # Numeric stores exact decimal values, eliminating floating-point rounding errors
    wallet_balance  = Column(Numeric(precision=12, scale=2), default=0.00)

    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())
