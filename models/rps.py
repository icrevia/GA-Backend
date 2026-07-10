from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class RPSMatch(Base):
    __tablename__ = "rps_matches"

    id = Column(Integer, primary_key=True, index=True)
    entry_fee = Column(Numeric(precision=12, scale=2), nullable=False)
    prize_pool = Column(Numeric(precision=12, scale=2), nullable=False)
    
    max_players = Column(Integer, default=2)
    
    status = Column(String, default="WAITING", index=True) # WAITING, PLAYING, COMPLETED, CANCELLED, REFUNDED
    
    winner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    participants = relationship("RPSParticipant", back_populates="match", cascade="all, delete-orphan")
    winner = relationship("User", foreign_keys=[winner_id])


class RPSParticipant(Base):
    __tablename__ = "rps_participants"

    id = Column(Integer, primary_key=True, index=True)
    match_id = Column(Integer, ForeignKey("rps_matches.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    move = Column(String, nullable=True) # ROCK, PAPER, SCISSORS, NONE
    
    status = Column(String, default="PLAYING") # WON, LOST, DRAW, ABANDONED, PLAYING
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    match = relationship("RPSMatch", back_populates="participants")
    user = relationship("User")
