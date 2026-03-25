from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.sql import func
from core.database import Base

class Tournament(Base):
    __tablename__ = "tournaments"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    game_name = Column(String, nullable=False)
    entry_fee = Column(Float, nullable=False)
    prize_pool = Column(Float, nullable=False)
    commission_percentage = Column(Float, default=10.0)
    match_time = Column(DateTime(timezone=True), nullable=False)
    
    # Room details (hidden until match starts and user joined)
    room_id = Column(String, nullable=True)
    room_password = Column(String, nullable=True)
    
    status = Column(String, default="UPCOMING") # UPCOMING, LIVE, COMPLETED
    match_type = Column(String, default="SOLO") # SOLO, DUO, SQUAD
    game_image_url = Column(String, nullable=True) # Banner image for the game
    winner_id = Column(Integer, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
