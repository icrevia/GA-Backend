from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class TournamentTeamMember(BaseModel):
    name: str
    uid: str
    level: Optional[int] = None

class TournamentBase(BaseModel):
    title: str
    game_name: str
    entry_fee: float
    prize_pool: float
    per_kill_prize: float = 0.0
    commission_percentage: Optional[float] = 10.0
    match_time: datetime
    match_type: str = "SOLO"
    game_image_url: Optional[str] = None
    max_slots: Optional[int] = 100

class TournamentCreate(TournamentBase):
    pass

class TournamentUpdate(BaseModel):
    title: Optional[str] = None
    game_name: Optional[str] = None
    entry_fee: Optional[float] = None
    prize_pool: Optional[float] = None
    commission_percentage: Optional[float] = None
    match_time: Optional[datetime] = None
    status: Optional[str] = None
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    winner_id: Optional[int] = None
    max_slots: Optional[int] = None

class ParticipantResponse(BaseModel):
    id: int
    user_id: int
    username: str
    avatar_url: Optional[str] = None
    slot_no: Optional[int] = None
    slot_label: Optional[str] = None
    team_members: list[TournamentTeamMember] = []
    # Team-based fields
    team_name: Optional[str] = None
    team_join_code: Optional[str] = None
    is_team_captain: bool = False

    class Config:
        from_attributes = True

class TournamentResponse(TournamentBase):
    id: int
    status: str
    created_at: datetime
    # hide room details unless queried by participant with proper status
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    winner_id: Optional[int] = None
    participants: Optional[list[ParticipantResponse]] = []
    joined_count: Optional[int] = 0  # computed — number of confirmed participants

    class Config:
        from_attributes = True

class TournamentJoinDeductionBreakdown(BaseModel):
    bonus_amount: float
    deposit_amount: float
    winning_amount: float
    total_deducted: float
    bonus_cap_amount: float
    bonus_usage_limit_percentage: float


class TournamentJoinResponse(BaseModel):
    message: str
    tournament_id: int
    new_wallet_balance: float
    slot_no: int
    slot_label: str
    team_members: list[TournamentTeamMember] = []
    # Team-based — only populated on CREATE
    team_join_code: Optional[str] = None
    team_name: Optional[str] = None
    is_team_captain: bool = False
    deduction_breakdown: Optional[TournamentJoinDeductionBreakdown] = None


class TournamentCancelResponse(BaseModel):
    message: str
    tournament_id: int
    cancelled_slots: int
    refund_percentage: int
    refund_amount: float
    refunded_to: str
    new_wallet_balance: float


class TournamentSlotResponse(BaseModel):
    slot_no: int
    slot_label: str
    status: str
    user_id: Optional[int] = None
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    game_username: Optional[str] = None
    game_uid: Optional[str] = None
    account_level: Optional[int] = None
    team_members: list[TournamentTeamMember] = []
    is_mine: bool = False
    # Team-based fields
    team_name: Optional[str] = None
    team_join_code: Optional[str] = None
    is_team_captain: bool = False


class TournamentSlotsBoardResponse(BaseModel):
    tournament_id: int
    max_slots: int
    booked_slots: int
    my_slot_no: Optional[int] = None
    my_slot_label: Optional[str] = None
    slots: list[TournamentSlotResponse]

class TournamentJoinRequest(BaseModel):
    # action: "CREATE" | "JOIN" | None (None → SOLO default)
    action: Optional[str] = None          # "CREATE" or "JOIN"
    team_name: Optional[str] = None       # Required when action="CREATE"
    join_code: Optional[str] = None       # Required when action="JOIN" (6-char team code)

    players: list[TournamentTeamMember] = []
    # Legacy fallback fields for older clients.
    game_username: Optional[str] = None
    game_uid: Optional[str] = None
    account_level: Optional[int] = Field(default=None, ge=1, le=100)


class TeamPreviewResponse(BaseModel):
    """Returned when user queries a join code before confirming JOIN."""
    team_join_code: str
    team_name: str
    captain_username: str
    current_members: int
    max_members: int
    is_full: bool
