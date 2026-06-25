from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

class LudoMatchBase(BaseModel):
    entry_fee: Decimal
    prize_pool: Decimal
    max_players: int

class LudoParticipantResponse(BaseModel):
    user_id: int
    color: str
    status: str

class LudoMatchResponse(LudoMatchBase):
    id: int
    status: str
    start_time: Optional[datetime] = None
    winner_id: Optional[int] = None
    participants: List[LudoParticipantResponse] = []

    class Config:
        from_attributes = True
