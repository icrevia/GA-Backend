from sqlalchemy import Column, Integer, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"

    id = Column(Integer, primary_key=True, index=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    tournament = relationship("Tournament", back_populates="participants")
    user = relationship("User")

    @property
    def username(self):
        return self.user.username
        
    @property
    def avatar_url(self):
        return self.user.avatar_url
    
    # Ensures a user can only join a tournament once
    # Unique constraint should be added in Alembic or __table_args__
