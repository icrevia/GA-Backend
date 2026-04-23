from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, Index
from sqlalchemy.sql import func
from core.database import Base


class Tournament(Base):
    __tablename__ = "tournaments"
    __table_args__ = (
        Index("ix_tournaments_status_match_time", "status", "match_time"),
    )

    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String, nullable=False)
    game_name   = Column(String, nullable=False)

    # Numeric(12,2) — exact decimal, no Decimal/float type mismatch with wallet_balance
    entry_fee            = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool           = Column(Numeric(precision=12, scale=2), nullable=False)
    commission_percentage = Column(Numeric(precision=5,  scale=2), default=10.00)
    per_kill_prize       = Column(Numeric(precision=12, scale=2), default=0.00, server_default="0.0")

    match_time = Column(DateTime(timezone=True), nullable=False, index=True)

    # Room details (hidden until match starts and user joined)
    room_id       = Column(String, nullable=True)
    room_password = Column(String, nullable=True)

    status     = Column(String,  default="UPCOMING", index=True)  # UPCOMING, LIVE, COMPLETED
    match_type = Column(String,  default="SOLO")       # SOLO, DUO, SQUAD
    game_image_url = Column(String, nullable=True)
    max_slots  = Column(Integer, default=100)
    winner_id  = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    from sqlalchemy.orm import relationship
    participants = relationship("TournamentParticipant", back_populates="tournament")
