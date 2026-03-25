from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float
from sqlalchemy.sql import func
from core.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    firebase_uid = Column(String, unique=True, index=True, nullable=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True) # Kept for backward compatibility or direct login if needed
    role = Column(String, default="USER") # USER or ADMIN
    upi_id = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)
    
    # Game IDs
    bgmi_id = Column(String, nullable=True)
    valorant_id = Column(String, nullable=True)
    freefire_id = Column(String, nullable=True)
    
    # Wallet balance cached for quick read, true source of truth is transactions
    wallet_balance = Column(Float, default=0.0)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
