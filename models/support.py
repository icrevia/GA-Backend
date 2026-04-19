from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, CheckConstraint, Index, Float
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
    blocked_by_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ended_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    attended_at = Column(DateTime, nullable=True)
    blocked_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    user_cleared_at = Column(DateTime, nullable=True)
    ended_by_role = Column(String(16), nullable=True)
    issue_type = Column(String(120), nullable=True)
    issue_ack_sent = Column(Boolean, default=False, nullable=False)
    is_user_blocked = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="ACTIVE", index=True)
    requires_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_utcnow_naive)

    # Explicit FK avoids ambiguity now that chat_sessions also references users via attended_by_admin_id.
    user = relationship("User", foreign_keys=[user_id])
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("length(content) <= 1000", name="ck_chat_messages_content_len"),
        CheckConstraint("(media_type IS NULL) OR (media_type IN ('photo', 'audio', 'video'))", name="ck_chat_messages_media_type"),
        Index("ix_chat_messages_thread_timestamp", "thread_user_id", "timestamp"),
        Index("ix_chat_messages_status", "thread_user_id", "is_admin", "is_read", "is_delivered"),
        Index("ix_chat_messages_media_expires_at", "media_expires_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=True)
    thread_user_id = Column(Integer, ForeignKey("users.id"), index=True) # The user whose thread this is
    sender_id = Column(Integer, ForeignKey("users.id"), index=True)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=_utcnow_naive)
    is_admin = Column(Boolean, default=False)
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime, nullable=True)
    is_delivered = Column(Boolean, default=False)
    delivered_at = Column(DateTime, nullable=True)
    media_type = Column(String(16), nullable=True)
    media_url = Column(Text, nullable=True)
    media_path = Column(Text, nullable=True)
    media_mime_type = Column(String(120), nullable=True)
    media_size_bytes = Column(Integer, nullable=True)
    media_duration_seconds = Column(Float, nullable=True)
    media_expires_at = Column(DateTime, nullable=True)

    session = relationship("ChatSession", back_populates="messages")
    thread_user = relationship("User", foreign_keys=[thread_user_id])
    sender = relationship("User", foreign_keys=[sender_id])
