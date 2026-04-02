import json

from sqlalchemy import Column, Integer, ForeignKey, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"
    __table_args__ = (
        UniqueConstraint("tournament_id", "user_id", name="uq_tournament_participant_user"),
        UniqueConstraint("tournament_id", "slot_no", name="uq_tournament_participant_slot"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Details provided during join
    game_username = Column(String, nullable=True) # Player's actual in-game name
    game_uid = Column(String, nullable=True)      # Player's game ID/UID
    team_members_raw = Column("team_members", Text, nullable=True)
    slot_no = Column(Integer, nullable=True)
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    tournament = relationship("Tournament", back_populates="participants")
    user = relationship("User")

    @property
    def username(self):
        return self.user.username
        
    @property
    def avatar_url(self):
        return self.user.avatar_url

    @property
    def slot_label(self):
        if not self.slot_no:
            return None
        return f"S{self.slot_no}"

    @property
    def team_members(self):
        """Returns normalized team members with backward compatibility for old rows."""
        parsed: list[dict[str, str]] = []
        if self.team_members_raw:
            try:
                data = json.loads(self.team_members_raw)
                if isinstance(data, list):
                    for member in data:
                        if not isinstance(member, dict):
                            continue
                        name = str(member.get("name") or "").strip()
                        uid = str(member.get("uid") or "").strip()
                        if name and uid:
                            parsed.append({"name": name, "uid": uid})
            except Exception:
                parsed = []

        if parsed:
            return parsed

        legacy_name = (self.game_username or "").strip()
        legacy_uid = (self.game_uid or "").strip()
        if legacy_name and legacy_uid:
            return [{"name": legacy_name, "uid": legacy_uid}]
        return []

    def set_team_members(self, team_members: list[dict[str, str]]) -> None:
        normalized: list[dict[str, str]] = []
        for member in team_members:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            uid = str(member.get("uid") or "").strip()
            if name and uid:
                normalized.append({"name": name, "uid": uid})

        self.team_members_raw = json.dumps(normalized, ensure_ascii=True) if normalized else None
        if normalized:
            # Keep legacy fields populated for old consumers.
            self.game_username = normalized[0]["name"]
            self.game_uid = normalized[0]["uid"]
