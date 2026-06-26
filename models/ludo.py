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
