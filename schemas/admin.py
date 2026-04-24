from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Union, Any, Literal
from datetime import datetime

MAX_NUMERIC_12_2 = Decimal("9999999999.99")

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
    user_ids: Optional[list[int]] = None

class UserStatusUpdate(BaseModel):
    is_active: bool


class UserWalletBucketsUpdate(BaseModel):
    deposit_balance: float = Field(..., ge=0, le=float(MAX_NUMERIC_12_2))
    winning_balance: float = Field(..., ge=0, le=float(MAX_NUMERIC_12_2))
    bonus_balance: float = Field(..., ge=0, le=float(MAX_NUMERIC_12_2))
    reason: Optional[str] = Field(default="Manual wallet bucket update", max_length=200)

    @model_validator(mode="after")
    def validate_total_wallet_limit(self):
        total = (
            Decimal(str(self.deposit_balance))
            + Decimal(str(self.winning_balance))
            + Decimal(str(self.bonus_balance))
        )
        if total > MAX_NUMERIC_12_2:
            raise ValueError(f"Total wallet balance cannot exceed {MAX_NUMERIC_12_2:.2f}")
        return self


class AdminWalletTransactionResponse(BaseModel):
    id: int
    amount: float
    transaction_type: str
    status: str
    reference_id: Optional[str] = None
    payment_mode: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RestrictionCreateRequest(BaseModel):
    user_id: int = Field(..., ge=1)
    scope: str = Field(..., min_length=4, max_length=20)
    page_key: Optional[str] = Field(default=None, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class BulkRestrictionCreateRequest(BaseModel):
    scope: str = Field(..., min_length=4, max_length=20)
    page_key: Optional[str] = Field(default=None, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class RestrictionUnlockRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class OtpLockResetRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class ActivityLockResetRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)

class TournamentRoomUpdate(BaseModel):
    room_id: str
    room_password: Optional[str] = None

class KillRewardEntry(BaseModel):
    user_id: int
    kills: int

class ManualRewardEntry(BaseModel):
    user_id: int
    amount: float
    kills: Optional[int] = 0
    rank: Optional[int] = None

class TournamentConclude(BaseModel):
    winner_id: Optional[str] = None
    kill_rewards: list[KillRewardEntry] = []
    manual_prizes: list[ManualRewardEntry] = []

class TournamentCreateAdmin(BaseModel):
    title: str
    game_name: str
    entry_fee: float
    prize_pool: float
    per_kill_prize: float = 0.0
    commission_percentage: float = Field(10.0, ge=0.0, le=100.0)
    match_type: str = "SOLO"
    match_time: str
    map_name: Optional[str] = None
    game_image_url: Optional[str] = None
    max_slots: Optional[int] = 100
    prize_distribution: Optional[list[Any]] = None

class TournamentUpdateAdmin(BaseModel):
    title: Optional[str] = None
    game_name: Optional[str] = None
    entry_fee: Optional[float] = None
    prize_pool: Optional[float] = None
    per_kill_prize: Optional[float] = None
    commission_percentage: Optional[float] = None
    match_type: Optional[str] = None
    match_time: Optional[str] = None
    map_name: Optional[str] = None
    game_image_url: Optional[str] = None
    max_slots: Optional[int] = None
    prize_distribution: Optional[list[Any]] = None
    status: Optional[str] = None


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


class AdminAccessSessionResponse(BaseModel):
    id: int
    user_id: int
    username: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    device_id: str
    device_name: Optional[str] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    is_active: bool
    created_at: datetime
    last_seen_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None
    is_current_admin: bool
    access_enabled: bool

    class Config:
        from_attributes = True


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
    page_key: str = Field(default="HOME")
    notes: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class BannerUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=120)
    image_url: Optional[str] = Field(default=None, min_length=5, max_length=500)
    redirect_url: Optional[str] = Field(default=None, max_length=500)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10_000)
    status: Optional[str] = None
    page_key: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=300)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class DepositBonusRule(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    label: Optional[str] = Field(default=None, max_length=80)
    min_amount: float = Field(..., gt=0)
    max_amount: Optional[float] = Field(default=None, gt=0)
    bonus_type: Literal["PERCENT", "FIXED"] = "PERCENT"
    bonus_value: float = Field(..., gt=0)
    max_bonus_amount: Optional[float] = Field(default=None, gt=0)
    is_active: bool = True

    @field_validator("bonus_type", mode="before")
    @classmethod
    def normalize_bonus_type(cls, value: str) -> str:
        if value is None:
            return "PERCENT"
        return str(value).strip().upper()

    @model_validator(mode="after")
    def validate_rule(self):
        if self.max_amount is not None and self.max_amount < self.min_amount:
            raise ValueError("max_amount must be greater than or equal to min_amount")

        if self.bonus_type == "PERCENT" and self.bonus_value > 100:
            raise ValueError("Percent bonus cannot exceed 100")

        return self


class DepositBonusConfigUpdate(BaseModel):
    enabled: bool = True
    rules: list[DepositBonusRule] = Field(default_factory=list)


class DepositBonusConfigResponse(BaseModel):
    enabled: bool
    rules: list[DepositBonusRule]


class ReferralRewardRule(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    label: Optional[str] = Field(default=None, max_length=80)
    trigger: Literal["REFERRAL_SIGNUP", "FIRST_SUCCESSFUL_DEPOSIT"] = "REFERRAL_SIGNUP"
    referred_user_reward: float = Field(default=0.0, ge=0)
    referrer_reward: float = Field(default=0.0, ge=0)
    min_recharge_amount: Optional[float] = Field(default=None, ge=0)
    max_reward_count_per_referrer: Optional[int] = Field(default=None, ge=1)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_rule(self):
        if self.trigger == "REFERRAL_SIGNUP" and self.min_recharge_amount not in (None, 0):
            raise ValueError("min_recharge_amount is only supported for FIRST_SUCCESSFUL_DEPOSIT trigger")

        if self.trigger == "FIRST_SUCCESSFUL_DEPOSIT" and self.min_recharge_amount is None:
            self.min_recharge_amount = 0.0

        if self.referred_user_reward <= 0 and self.referrer_reward <= 0:
            raise ValueError("At least one of referred_user_reward or referrer_reward must be greater than 0")

        return self


class ReferralRewardConfigUpdate(BaseModel):
    enabled: bool = True
    rules: list[ReferralRewardRule] = Field(default_factory=list)


class ReferralRewardConfigResponse(BaseModel):
    enabled: bool
    rules: list[ReferralRewardRule]


