from pydantic import BaseModel, Field
from typing import Optional, Union, Any
from datetime import datetime

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


class UserWalletBucketsUpdate(BaseModel):
    deposit_balance: float = Field(..., ge=0)
    winning_balance: float = Field(..., ge=0)
    bonus_balance: float = Field(..., ge=0)
    reason: Optional[str] = Field(default="Manual wallet bucket update", max_length=200)


class RestrictionCreateRequest(BaseModel):
    user_id: int = Field(..., ge=1)
    scope: str = Field(..., min_length=4, max_length=20)
    page_key: Optional[str] = Field(default=None, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class RestrictionUnlockRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class OtpLockResetRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)

class TournamentRoomUpdate(BaseModel):
    room_id: str
    room_password: Optional[str] = None

class KillRewardEntry(BaseModel):
    """Per-member kill data for DUO/SQUAD prize distribution."""
    user_id: int
    kills: int = Field(0, ge=0)


class TournamentConclude(BaseModel):
    # ── SOLO: provide winner_id only ──────────────────────────────
    winner_id: Optional[Union[int, str]] = None

    # ── DUO / SQUAD: provide winning_team_code + kill data ────────
    winning_team_code: Optional[str] = None  # 6-char join code of the winning team
    kill_value: float = Field(0.0, ge=0)     # ₹ per kill (e.g. 10.0)
    kill_rewards: list[KillRewardEntry] = []  # kills per member

class TournamentCreateAdmin(BaseModel):
    title: str
    game_name: str
    entry_fee: float
    prize_pool: float
    commission_percentage: float = Field(10.0, ge=0.0, le=100.0)
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


class PromoCreateRequest(BaseModel):
    code: str = Field(..., min_length=3, max_length=40)
    reward_amount: float = Field(..., gt=0)
    max_uses: int = Field(100, ge=1, le=1_000_000)
    status: str = Field(default="ACTIVE")
    notes: Optional[str] = Field(default=None, max_length=300)
    expires_at: Optional[datetime] = None


class PromoUpdateRequest(BaseModel):
    code: Optional[str] = Field(default=None, min_length=3, max_length=40)
    reward_amount: Optional[float] = Field(default=None, gt=0)
    discount: Optional[float] = Field(default=None, gt=0)
    max_uses: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    status: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=300)
    expires_at: Optional[datetime] = None


class BannerCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    image_url: str = Field(..., min_length=5, max_length=500)
    redirect_url: Optional[str] = Field(default=None, max_length=500)
    sort_order: int = Field(default=0, ge=0, le=10_000)
    status: str = Field(default="ACTIVE")
    notes: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class BannerUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=120)
    image_url: Optional[str] = Field(default=None, min_length=5, max_length=500)
    redirect_url: Optional[str] = Field(default=None, max_length=500)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10_000)
    status: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
