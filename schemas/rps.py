from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

class RPSMatchBase(BaseModel):
    entry_fee: Decimal
    prize_pool: Decimal
    max_players: int

class RPSParticipantResponse(BaseModel):
    user_id: int
    move: Optional[str] = None
    status: str

class RPSMatchResponse(RPSMatchBase):
    id: int
    status: str
    start_time: Optional[datetime] = None
    winner_id: Optional[int] = None
    participants: List[RPSParticipantResponse] = []

    class Config:
        from_attributes = True

class RPSConfigResponse(BaseModel):
    is_enabled: bool
    entry_fee: float
    prize_multiplier: float
    turn_timer_seconds: int
    match_duration_minutes: int
    draw_refund_percentage: float
