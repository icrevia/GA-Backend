from sqlalchemy import Column, Integer, ForeignKey, DateTime, String, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"
    __table_args__ = (
        UniqueConstraint("tournament_id", "user_id", name="uq_tournament_participant_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Details provided during join
    game_username = Column(String, nullable=True) # Player's actual in-game name
    game_uid = Column(String, nullable=True)      # Player's game ID/UID
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    tournament = relationship("Tournament", back_populates="participants")
    user = relationship("User")

    @property
    def username(self):
        return self.user.username
        
    @property
    def avatar_url(self):
        return self.user.avatar_url
