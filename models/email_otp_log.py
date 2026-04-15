from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from core.database import Base


class EmailOtpLog(Base):
    __tablename__ = "email_otp_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    email = Column(String(320), nullable=True, index=True)
    phone_number = Column(String(20), nullable=True, index=True)
    source = Column(String(32), nullable=False, default="LOGIN")
    event_type = Column(String(16), nullable=False, index=True)  # SEND | VERIFY
    status = Column(String(24), nullable=False, index=True)
    message = Column(Text, nullable=True)
    client_ip = Column(String(64), nullable=True)
    user_agent = Column(String(220), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
