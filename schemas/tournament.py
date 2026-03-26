from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class TournamentBase(BaseModel):
    title: str
    game_name: str
    entry_fee: float
    prize_pool: float
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

class ParticipantResponse(BaseModel):
    id: int
    user_id: int
    username: str
    avatar_url: Optional[str] = None

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

    class Config:
        from_attributes = True

class TournamentJoinResponse(BaseModel):
    message: str
    tournament_id: int
    new_wallet_balance: float

class TournamentJoinRequest(BaseModel):
    game_username: str
    game_uid: str
