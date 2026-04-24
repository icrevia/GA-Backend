import json
from sqlalchemy import Column, Integer, Boolean, ForeignKey, DateTime, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base

class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"
    __table_args__ = (
        UniqueConstraint("tournament_id", "user_id", name="uq_tournament_participant_user"),
        Index("uq_tournament_participant_slot_idx", "tournament_id", "slot_no", unique=True, postgresql_where=text("slot_no IS NOT NULL")),
        Index("ix_tp_team_join_code", "team_join_code", postgresql_where=text("team_join_code IS NOT NULL")),
    )

    id = Column(Integer, primary_key=True, index=True)
    tournament_id = Column(Integer, ForeignKey("tournaments.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Details provided during join
    game_username = Column(String, nullable=True) # Player's actual in-game name
    game_uid = Column(String, nullable=True)      # Player's game ID/UID
    account_level = Column(Integer, nullable=True)
    team_members_raw = Column("team_members", Text, nullable=True)
    slot_no = Column(Integer, nullable=True)

    # Team-based fields (DUO / SQUAD)
    team_name       = Column(String, nullable=True)  # Team name chosen by captain
    team_join_code  = Column(String, nullable=True, index=True)  # 6-char code shared with teammates
    # Results (populated during conclude_tournament)
    participant_rank = Column(Integer, nullable=True)
    kills        = Column(Integer, default=0)
    prize_amount = Column(String, nullable=True) # Stored as string to handle decimals/formating if needed, or Numeric
    
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    tournament = relationship("Tournament", back_populates="participants")
    user = relationship("User")

    @property
    def username(self):
        return self.user.username
        
    @property
    def avatar_url(self):
        return self.user.profile_pic if self.user else None

    @property
    def bio(self):
        return self.user.bio if self.user else None

    @property
    def slot_label(self):
        if not self.slot_no:
            return None
        return f"S{self.slot_no}"

    @property
    def team_members(self):
        """Returns normalized team members with backward compatibility for old rows."""
        parsed: list[dict[str, object]] = []
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
                            level_value = member.get("level")
                            level: int | None = None
                            if isinstance(level_value, int):
                                level = level_value
                            elif isinstance(level_value, str) and level_value.strip().isdigit():
                                level = int(level_value.strip())

                            normalized_member: dict[str, object] = {"name": name, "uid": uid}
                            if level is not None:
                                normalized_member["level"] = level
                            parsed.append(normalized_member)
            except Exception:
                parsed = []

        if parsed:
            return parsed

        legacy_name = (self.game_username or "").strip()
        legacy_uid = (self.game_uid or "").strip()
        if legacy_name and legacy_uid:
            fallback_member: dict[str, object] = {"name": legacy_name, "uid": legacy_uid}
            if self.account_level is not None:
                fallback_member["level"] = int(self.account_level)
            return [fallback_member]
        return []

    def set_team_members(self, team_members: list[dict[str, object]]) -> None:
        normalized: list[dict[str, object]] = []
        for member in team_members:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            uid = str(member.get("uid") or "").strip()
            if name and uid:
                level_value = member.get("level")
                level: int | None = None
                if isinstance(level_value, int):
                    level = level_value
                elif isinstance(level_value, str) and level_value.strip().isdigit():
                    level = int(level_value.strip())

                normalized_member: dict[str, object] = {"name": name, "uid": uid}
                if level is not None:
                    normalized_member["level"] = level
                normalized.append(normalized_member)

        self.team_members_raw = json.dumps(normalized, ensure_ascii=True) if normalized else None
        if normalized:
            # Keep legacy fields populated for old consumers.
            primary = normalized[0]
            self.game_username = str(primary.get("name") or "")
            self.game_uid = str(primary.get("uid") or "")
            level_value = primary.get("level")
            self.account_level = int(level_value) if isinstance(level_value, int) else self.account_level
