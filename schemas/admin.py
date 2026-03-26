from pydantic import BaseModel
from typing import Optional, Union, Any

class SystemConfigResponse(BaseModel):
    id: int
    config_key: str
    config_value: str
    
    class Config:
        from_attributes = True

class SystemConfigUpdate(BaseModel):
    key: str
    value: str

class NotificationSendRequest(BaseModel):
    title: str
    body: str
    topic: str = "all"

class UserStatusUpdate(BaseModel):
    is_active: bool

class TournamentRoomUpdate(BaseModel):
    room_id: str
    room_password: Optional[str] = None

class TournamentConclude(BaseModel):
    winner_id: Union[int, str]

class TournamentCreateAdmin(BaseModel):
    title: str
    game_name: str
    entry_fee: float
    prize_pool: float
    match_type: str
    match_time: str
    game_image_url: Optional[str] = None
