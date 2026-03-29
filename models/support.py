from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, CheckConstraint, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from core.database import Base

class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String, default="ACTIVE")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("length(content) <= 1000", name="ck_chat_messages_content_len"),
        Index("ix_chat_messages_session_timestamp", "session_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"))
    sender_id = Column(Integer, ForeignKey("users.id"))
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_admin = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)

    session = relationship("ChatSession", back_populates="messages")
