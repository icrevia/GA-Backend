from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, CheckConstraint, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from core.database import Base


def _utcnow_naive() -> datetime:
    # chat_sessions/chat_messages columns are TIMESTAMP WITHOUT TIME ZONE
    # so defaults must be naive datetime values.
    return datetime.utcnow()

class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    attended_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    attended_at = Column(DateTime, nullable=True)
    status = Column(String, default="ACTIVE")
    requires_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_utcnow_naive)

    # Explicit FK avoids ambiguity now that chat_sessions also references users via attended_by_admin_id.
    user = relationship("User", foreign_keys=[user_id])
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
    timestamp = Column(DateTime, default=_utcnow_naive)
    is_admin = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)

    session = relationship("ChatSession", back_populates="messages")
