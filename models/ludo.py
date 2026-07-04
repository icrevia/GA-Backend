from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class LudoMatch(Base):
    __tablename__ = "ludo_matches"

    id = Column(Integer, primary_key=True, index=True)
    entry_fee = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool = Column(Numeric(precision=12, scale=2), nullable=False)
    
    max_players = Column(Integer, default=2)
    
    status = Column(String, default="WAITING", index=True) # WAITING, PLAYING, COMPLETED, CANCELLED
    
    winner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    participants = relationship("LudoParticipant", back_populates="match", cascade="all, delete-orphan")
    winner = relationship("User", foreign_keys=[winner_id])


class LudoParticipant(Base):
    __tablename__ = "ludo_participants"

    id = Column(Integer, primary_key=True, index=True)
    match_id = Column(Integer, ForeignKey("ludo_matches.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    color = Column(String, nullable=False) # RED, BLUE, GREEN, YELLOW
    
    status = Column(String, default="PLAYING") # WON, LOST, ABANDONED, PLAYING
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    match = relationship("LudoMatch", back_populates="participants")
    user = relationship("User")


class LudoChallenge(Base):
    """
    Challenge Mode — player-created 1v1 challenges with custom prize pools.
    Flow: OPEN → WAITING_SYNC → PLAYING → COMPLETED
          OPEN → EXPIRED (1hr no opponent, 100% refund)
          WAITING_SYNC → CANCELLED (10min sync timeout, 30% penalty on late player)
    """
    __tablename__ = "ludo_challenges"

    id = Column(Integer, primary_key=True, index=True)

    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    creator_deductions = Column(JSON, nullable=True)
    creator_synced = Column(Boolean, default=False)

    opponent_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    opponent_deductions = Column(JSON, nullable=True)
    opponent_synced = Column(Boolean, default=False)

    entry_fee = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool = Column(Numeric(precision=12, scale=2), nullable=False)

    # OPEN | WAITING_SYNC | PLAYING | COMPLETED | CANCELLED | EXPIRED
    status = Column(String, default="OPEN", index=True)

    expires_at = Column(DateTime(timezone=True), nullable=False)       # +1hr from creation
    sync_deadline = Column(DateTime(timezone=True), nullable=True)     # +10min from first Play Now tap

    match_id = Column(Integer, ForeignKey("ludo_matches.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    creator  = relationship("User", foreign_keys=[creator_id])
    opponent = relationship("User", foreign_keys=[opponent_id])
    match    = relationship("LudoMatch", foreign_keys=[match_id])
