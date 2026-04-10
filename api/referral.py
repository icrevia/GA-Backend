from decimal import Decimal
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user_referral
from core.database import get_db_sync as get_db
from models.user import User
from models.wallet import WalletTransaction
from services.notifications import add_user_notification

router = APIRouter()


REFERRAL_MISSIONS: List[dict] = [
    {
        "key": "RECHARGE_RUNNER",
        "title": "Recharge Runner",
        "description": "1 referred friend must complete a recharge of INR 50 or more.",
        "target_referrals": 1,
        "reward_amount": 25.0,
    },
    {
        "key": "SQUAD_STREAK",
        "title": "Squad Streak",
        "description": "3 referred friends each complete a recharge of INR 50 or more.",
        "target_referrals": 3,
        "reward_amount": 50.0,
    },
]


REFERRAL_BASE_SIGNUP_REWARD = Decimal("2.00")
REFERRAL_QUALIFYING_RECHARGE = Decimal("50.00")


MISSION_BY_KEY = {mission["key"]: mission for mission in REFERRAL_MISSIONS}
MISSION_REF_PREFIX = "REF_MISSION"


class ReferralMission(BaseModel):
    key: str
    title: str
    description: str
    target_referrals: int
    reward_amount: float
    progress_referrals: int
    completed: bool
    claimed: bool
    claimable: bool


class NextMilestone(BaseModel):
    key: str
    title: str
    reward_amount: float
    target_referrals: int
    remaining_referrals: int


class ReferredUser(BaseModel):
    user_id: int
    username: str
    joined_at: str | None
    qualified_for_missions: bool


class ReferralStats(BaseModel):
    referral_code: str
    total_referrals: int
    qualified_referrals: int
    total_earned: float
    base_referral_reward: float
    qualifying_recharge_amount: float
    claimable_rewards_total: float
    missions: List[ReferralMission]
    next_milestone: NextMilestone | None
    recent_referrals: List[ReferredUser]


class MissionClaimResponse(BaseModel):
    message: str
    mission_key: str
    reward_amount: float
    wallet_balance: float
    stats: ReferralStats


def _count_referrals(db: Session, user_id: int) -> int:
    return db.query(User).filter(User.referred_by_id == user_id).count()


def _get_qualified_referral_user_ids(db: Session, user_id: int) -> set[int]:
    qualifying_rows = (
        db.query(User.id)
        .join(WalletTransaction, WalletTransaction.user_id == User.id)
        .filter(
            User.referred_by_id == user_id,
            WalletTransaction.transaction_type == "ADD_MONEY",
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.amount >= REFERRAL_QUALIFYING_RECHARGE,
        )
        .distinct()
        .all()
    )
    return {int(uid) for (uid,) in qualifying_rows}


def _extract_claimed_mission_keys(db: Session, user_id: int) -> set[str]:
    prefix = f"{MISSION_REF_PREFIX}_{user_id}_"
    refs = db.query(WalletTransaction.reference_id).filter(
        WalletTransaction.user_id == user_id,
        WalletTransaction.transaction_type == "REFERRAL_MISSION_REWARD",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.reference_id.like(f"{prefix}%"),
    ).all()

    keys: set[str] = set()
    for (reference_id,) in refs:
        if not reference_id:
            continue
        if reference_id.startswith(prefix):
            keys.add(reference_id[len(prefix):])
    return keys


def _mission_reference_id(user_id: int, mission_key: str) -> str:
    return f"{MISSION_REF_PREFIX}_{user_id}_{mission_key}"


def _ensure_referral_code(current_user: User, db: Session) -> str:
    code = (current_user.referral_code or "").strip().upper()
    if code:
        if current_user.referral_code != code:
            current_user.referral_code = code
            db.commit()
            db.refresh(current_user)
        return code

    base_code = f"ZP{current_user.id:06d}"
    candidate = base_code
    sequence = 1

    while True:
        owner = db.query(User.id).filter(User.referral_code == candidate).first()
        if not owner or owner[0] == current_user.id:
            break
        candidate = f"{base_code}{sequence:02d}"
        sequence += 1

    current_user.referral_code = candidate
    db.commit()
    db.refresh(current_user)
    return candidate


def _build_referral_stats(current_user: User, db: Session) -> ReferralStats:
    referral_code = _ensure_referral_code(current_user, db)
    total_referrals = _count_referrals(db, current_user.id)
    qualified_referral_ids = _get_qualified_referral_user_ids(db, current_user.id)
    qualified_referrals = len(qualified_referral_ids)

    earned_rows = db.query(WalletTransaction.amount).filter(
        WalletTransaction.user_id == current_user.id,
        WalletTransaction.transaction_type.in_(["REFERRAL_REWARD", "REFERRAL_MISSION_REWARD"]),
        WalletTransaction.status == "SUCCESS",
    ).all()
    total_earned = float(sum((Decimal(str(amount)) for (amount,) in earned_rows), Decimal("0")))

    recent_referral_rows = db.query(User.id, User.username, User.created_at).filter(
        User.referred_by_id == current_user.id,
    ).order_by(User.created_at.desc(), User.id.desc()).limit(20).all()

    recent_referrals = [
        ReferredUser(
            user_id=row.id,
            username=row.username,
            joined_at=row.created_at.isoformat() if row.created_at else None,
            qualified_for_missions=row.id in qualified_referral_ids,
        )
        for row in recent_referral_rows
    ]

    claimed_keys = _extract_claimed_mission_keys(db, current_user.id)

    mission_items: List[ReferralMission] = []
    claimable_total = Decimal("0")
    next_milestone: NextMilestone | None = None

    for mission in REFERRAL_MISSIONS:
        key = mission["key"]
        target = int(mission["target_referrals"])
        reward = float(mission["reward_amount"])
        progress = min(qualified_referrals, target)
        completed = qualified_referrals >= target
        claimed = key in claimed_keys
        claimable = completed and not claimed

        if claimable:
            claimable_total += Decimal(str(reward))

        if next_milestone is None and not completed:
            next_milestone = NextMilestone(
                key=key,
                title=mission["title"],
                reward_amount=reward,
                target_referrals=target,
                remaining_referrals=target - qualified_referrals,
            )

        mission_items.append(
            ReferralMission(
                key=key,
                title=mission["title"],
                description=mission["description"],
                target_referrals=target,
                reward_amount=reward,
                progress_referrals=progress,
                completed=completed,
                claimed=claimed,
                claimable=claimable,
            )
        )

    return ReferralStats(
        referral_code=referral_code,
        total_referrals=total_referrals,
        qualified_referrals=qualified_referrals,
        total_earned=total_earned,
        base_referral_reward=float(REFERRAL_BASE_SIGNUP_REWARD),
        qualifying_recharge_amount=float(REFERRAL_QUALIFYING_RECHARGE),
        claimable_rewards_total=float(claimable_total),
        missions=mission_items,
        next_milestone=next_milestone,
        recent_referrals=recent_referrals,
    )


@router.get("/stats", response_model=ReferralStats)
def get_referral_stats(
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db)
) -> Any:
    return _build_referral_stats(current_user, db)


@router.post("/missions/{mission_key}/claim", response_model=MissionClaimResponse)
def claim_referral_mission(
    mission_key: str,
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
) -> Any:
    normalized_key = mission_key.strip().upper()
    mission = MISSION_BY_KEY.get(normalized_key)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    qualified_referrals = len(_get_qualified_referral_user_ids(db, current_user.id))
    target_referrals = int(mission["target_referrals"])
    reward_amount = Decimal(str(mission["reward_amount"]))

    if qualified_referrals < target_referrals:
        remaining = target_referrals - qualified_referrals
        raise HTTPException(
            status_code=400,
            detail=(
                f"Mission not completed yet. Need {remaining} more qualified referral(s). "
                f"Each referred user must complete recharge of INR {int(REFERRAL_QUALIFYING_RECHARGE)}+"
            ),
        )

    reference_id = _mission_reference_id(current_user.id, normalized_key)
    existing_claim = db.query(WalletTransaction.id).filter(
        WalletTransaction.reference_id == reference_id,
        WalletTransaction.status == "SUCCESS",
    ).first()
    if existing_claim:
        raise HTTPException(status_code=409, detail="Mission already claimed")

    current_balance = Decimal(str(current_user.wallet_balance or 0))
    current_user.wallet_balance = current_balance + reward_amount
    db.add(WalletTransaction(
        user_id=current_user.id,
        amount=reward_amount,
        transaction_type="REFERRAL_MISSION_REWARD",
        status="SUCCESS",
        reference_id=reference_id,
    ))
    db.commit()
    db.refresh(current_user)

    add_user_notification(
        db,
        current_user.id,
        "Referral Mission Completed",
        f"{mission['title']} complete. ₹{float(reward_amount):.0f} added to your wallet.",
        "WALLET",
    )

    return {
        "message": "Mission reward claimed successfully",
        "mission_key": normalized_key,
        "reward_amount": float(reward_amount),
        "wallet_balance": float(current_user.wallet_balance or 0),
        "stats": _build_referral_stats(current_user, db),
    }
