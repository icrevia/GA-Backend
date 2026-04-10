from decimal import Decimal
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.deps import get_current_user_referral
from core.database import get_db_sync as get_db
from models.user import User
from models.wallet import WalletTransaction
from services.referral_codes import (
    generate_unique_referral_code_sync,
    is_username_aligned_referral_code,
)
from services.referral_rewards import (
    FIRST_DEPOSIT_MATCH_MULTIPLIER,
    REFERRAL_HIGH_BONUS_MAX,
    REFERRAL_HIGH_BONUS_MIN,
    REFERRAL_HIGH_BAND_PROBABILITY,
    REFERRAL_JACKPOT_BONUS_MAX,
    REFERRAL_JACKPOT_BONUS_MIN,
    REFERRAL_JACKPOT_BAND_PROBABILITY,
    REFERRAL_LOW_BONUS_MAX,
    REFERRAL_LOW_BONUS_MIN,
    REFERRAL_LOW_BAND_PROBABILITY,
    REFERRAL_REWARD_TX_TYPE,
)

router = APIRouter()


class ReferralRewardPolicy(BaseModel):
    per_referral_min: float
    per_referral_max: float
    low_band_min: float
    low_band_max: float
    low_band_probability: float
    high_band_min: float
    high_band_max: float
    high_band_probability: float
    jackpot_band_min: float
    jackpot_band_max: float
    jackpot_band_probability: float
    first_deposit_match_multiplier: float


class ReferredUser(BaseModel):
    user_id: int
    username: str
    joined_at: str | None
    has_first_deposit: bool


class ReferralStats(BaseModel):
    referral_code: str
    total_referrals: int
    activated_referrals: int
    pending_referrals: int
    total_earned: float
    claimable_rewards_total: float = 0.0
    missions: List[dict] = Field(default_factory=list)
    next_milestone: dict | None = None
    reward_policy: ReferralRewardPolicy
    recent_referrals: List[ReferredUser]


class MissionClaimResponse(BaseModel):
    message: str
    mission_key: str
    reward_amount: float
    wallet_balance: float
    stats: ReferralStats


def _count_referrals(db: Session, user_id: int) -> int:
    return db.query(User).filter(User.referred_by_id == user_id).count()


def _get_activated_referral_user_ids(db: Session, user_id: int) -> set[int]:
    rows = (
        db.query(User.id)
        .join(WalletTransaction, WalletTransaction.user_id == User.id)
        .filter(
            User.referred_by_id == user_id,
            WalletTransaction.transaction_type == "ADD_MONEY",
            WalletTransaction.status == "SUCCESS",
        )
        .distinct()
        .all()
    )
    return {int(uid) for (uid,) in rows}


def _ensure_referral_code(current_user: User, db: Session) -> str:
    code = (current_user.referral_code or "").strip().upper()
    should_regenerate = not is_username_aligned_referral_code(current_user.username, code)

    if code and not should_regenerate:
        if current_user.referral_code != code:
            current_user.referral_code = code
            db.commit()
            db.refresh(current_user)
        return code

    current_user.referral_code = generate_unique_referral_code_sync(
        db=db,
        username=current_user.username,
        user_id=current_user.id,
    )
    db.commit()
    db.refresh(current_user)
    return current_user.referral_code


def _build_reward_policy() -> ReferralRewardPolicy:
    return ReferralRewardPolicy(
        per_referral_min=float(REFERRAL_LOW_BONUS_MIN),
        per_referral_max=float(REFERRAL_JACKPOT_BONUS_MAX),
        low_band_min=float(REFERRAL_LOW_BONUS_MIN),
        low_band_max=float(REFERRAL_LOW_BONUS_MAX),
        low_band_probability=REFERRAL_LOW_BAND_PROBABILITY,
        high_band_min=float(REFERRAL_HIGH_BONUS_MIN),
        high_band_max=float(REFERRAL_HIGH_BONUS_MAX),
        high_band_probability=REFERRAL_HIGH_BAND_PROBABILITY,
        jackpot_band_min=float(REFERRAL_JACKPOT_BONUS_MIN),
        jackpot_band_max=float(REFERRAL_JACKPOT_BONUS_MAX),
        jackpot_band_probability=REFERRAL_JACKPOT_BAND_PROBABILITY,
        first_deposit_match_multiplier=float(FIRST_DEPOSIT_MATCH_MULTIPLIER),
    )


def _build_referral_stats(current_user: User, db: Session) -> ReferralStats:
    referral_code = _ensure_referral_code(current_user, db)
    total_referrals = _count_referrals(db, current_user.id)
    activated_referral_ids = _get_activated_referral_user_ids(db, current_user.id)

    earned_rows = db.query(WalletTransaction.amount).filter(
        WalletTransaction.user_id == current_user.id,
        WalletTransaction.transaction_type.in_([
            REFERRAL_REWARD_TX_TYPE,
            "REFERRAL_MISSION_REWARD",  # legacy data kept for historical totals
        ]),
        WalletTransaction.status == "SUCCESS",
    ).all()
    total_earned = float(sum((Decimal(str(amount)) for (amount,) in earned_rows), Decimal("0")))

    recent_referral_rows = (
        db.query(User.id, User.username, User.created_at)
        .filter(User.referred_by_id == current_user.id)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(20)
        .all()
    )

    recent_referrals = [
        ReferredUser(
            user_id=row.id,
            username=row.username,
            joined_at=row.created_at.isoformat() if row.created_at else None,
            has_first_deposit=row.id in activated_referral_ids,
        )
        for row in recent_referral_rows
    ]

    return ReferralStats(
        referral_code=referral_code,
        total_referrals=total_referrals,
        activated_referrals=len(activated_referral_ids),
        pending_referrals=max(total_referrals - len(activated_referral_ids), 0),
        total_earned=total_earned,
        claimable_rewards_total=0.0,
        missions=[],
        next_milestone=None,
        reward_policy=_build_reward_policy(),
        recent_referrals=recent_referrals,
    )


@router.get("/stats", response_model=ReferralStats)
def get_referral_stats(
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
) -> Any:
    return _build_referral_stats(current_user, db)


@router.post("/missions/{mission_key}/claim", response_model=MissionClaimResponse)
def claim_referral_mission(
    mission_key: str,
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
) -> Any:
    _ = (mission_key, current_user, db)
    raise HTTPException(
        status_code=410,
        detail=(
            "Referral missions are retired. Rewards are auto-credited when your referred user "
            "completes the first successful deposit."
        ),
    )
