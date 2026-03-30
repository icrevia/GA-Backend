from pydantic import BaseModel, Field
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
    max_slots: Optional[int] = 100


class DeveloperOtpRequestResponse(BaseModel):
    otp_required: bool = True
    message: str
    expires_in_seconds: int = 0
    resend_cooldown_seconds: int = 0


class DeveloperOtpVerifyRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=8, pattern=r"^\d+$")


class DeveloperOtpVerifyResponse(BaseModel):
    verified: bool
    developer_otp_token: Optional[str] = None
    expires_in_seconds: int = 0
    message: str


class DeveloperOtpStatusResponse(BaseModel):
    otp_required: bool
    verified: bool
    expires_in_seconds: int = 0
