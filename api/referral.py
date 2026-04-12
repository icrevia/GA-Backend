from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
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
from services.wallet_balances import (
    WALLET_BUCKET_DEPOSIT,
    credit_wallet,
    get_wallet_breakdown,
    to_money,
)

router = APIRouter()

SCRATCH_CARD_VALIDITY_DAYS = 7           # each earned card is valid for 7 days
MATCHES_PER_SCRATCH_CARD   = 5           # 1 card per 5 completed matches

SCRATCH_CARD_REWARD_TX_TYPE = "SCRATCH_CARD_REWARD"
SCRATCH_CARD_REVEAL_TX_TYPE = "SCRATCH_CARD_REVEAL"



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


class ScratchCard(BaseModel):
    card_id: str
    reward_amount: float
    is_scratched: bool
    valid_for_days: int
    expires_at: str


class ScratchCardDeckResponse(BaseModel):
    total_cards: int
    active_cards: int
    revealed_cards: int
    cards: List[ScratchCard]


class ScratchCardRevealResponse(BaseModel):
    message: str
    card_id: str
    reward_amount: float
    credited_to: str
    wallet_balance: float
    deposit_balance: float
    winning_balance: float
    bonus_balance: float


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


def _to_utc_naive(value: datetime | None) -> datetime:
    if value is None:
        return datetime.utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _scratch_cycle_window(current_user: User, now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    now = now_utc or datetime.utcnow()
    joined_utc = _to_utc_naive(current_user.created_at)
    anchor = datetime(joined_utc.year, joined_utc.month, joined_utc.day)
    elapsed_days = max((now.date() - anchor.date()).days, 0)
    cycle_index = elapsed_days // SCRATCH_CARD_VALIDITY_DAYS
    cycle_start = anchor + timedelta(days=cycle_index * SCRATCH_CARD_VALIDITY_DAYS)
    cycle_end = cycle_start + timedelta(days=SCRATCH_CARD_VALIDITY_DAYS)
    return cycle_start, cycle_end


def _scratch_reward_for_card(user_id: int, card_serial: int) -> Decimal:
    """98% -> ₹1.00 or ₹1.50 (50/50), 2% -> ₹2.00."""
    seed = f"sc:{user_id}:{card_serial}".encode()
    digest = hashlib.sha256(seed).digest()
    roll = digest[0] % 100          # 0-99
    if roll < 98:
        sub = digest[1] % 2         # 0=₹1.00, 1=₹1.50
        return Decimal("1.00") + Decimal("0.50") * sub
    return Decimal("2.00")


def _count_completed_matches(user_id: int, db: Session) -> int:
    from models.participant import TournamentParticipant
    from models.tournament import Tournament
    return (
        db.query(TournamentParticipant)
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .filter(
            TournamentParticipant.user_id == user_id,
            Tournament.status == "COMPLETED",
        )
        .count()
    )


def _scratch_card_reference(user_id: int, card_id: str) -> str:
    return f"SCRATCH_CARD_{user_id}_{card_id}"


def _build_scratch_card_deck(current_user: User, db: Session) -> ScratchCardDeckResponse:
    """One scratch card per MATCHES_PER_SCRATCH_CARD completed matches.
    Each card is valid for SCRATCH_CARD_VALIDITY_DAYS days.
    """
    now_utc = datetime.utcnow()
    completed = _count_completed_matches(current_user.id, db)
    total_earned = completed // MATCHES_PER_SCRATCH_CARD

    joined_utc = _to_utc_naive(current_user.created_at)
    anchor = datetime(joined_utc.year, joined_utc.month, joined_utc.day)

    planned_cards: list[tuple[str, Decimal, str, str, int]] = []
    for serial in range(1, total_earned + 1):
        card_id  = f"mc-{current_user.id}-{serial}"
        reward   = to_money(_scratch_reward_for_card(current_user.id, serial))
        ref_id   = _scratch_card_reference(current_user.id, card_id)
        # An earned card becomes valid from the day it was earned
        earned_after_match = serial * MATCHES_PER_SCRATCH_CARD
        earned_on = anchor + timedelta(days=earned_after_match)
        expires   = earned_on + timedelta(days=SCRATCH_CARD_VALIDITY_DAYS)
        valid_days = max((expires.date() - now_utc.date()).days, 0)
        planned_cards.append((card_id, reward, ref_id, expires.isoformat() + "Z", valid_days))

    # Only show non-expired cards
    active_planned = [(cid, r, rid, exp, vd) for cid, r, rid, exp, vd in planned_cards if vd > 0]

    reference_ids = [rid for _, _, rid, _, _ in active_planned]
    scratched_refs: set[str] = set()
    if reference_ids:
        rows = (
            db.query(WalletTransaction.reference_id)
            .filter(
                WalletTransaction.user_id == current_user.id,
                WalletTransaction.status == "SUCCESS",
                WalletTransaction.reference_id.in_(reference_ids),
                WalletTransaction.transaction_type.in_(
                    [SCRATCH_CARD_REWARD_TX_TYPE, SCRATCH_CARD_REVEAL_TX_TYPE]
                ),
            )
            .all()
        )
        scratched_refs = {ref for (ref,) in rows if ref}

    cards = [
        ScratchCard(
            card_id=card_id,
            reward_amount=float(reward),
            is_scratched=ref_id in scratched_refs,
            valid_for_days=valid_days,
            expires_at=expires_at,
        )
        for card_id, reward, ref_id, expires_at, valid_days in active_planned
    ]
    revealed = sum(1 for c in cards if c.is_scratched)

    return ScratchCardDeckResponse(
        total_cards=len(cards),
        active_cards=max(len(cards) - revealed, 0),
        revealed_cards=revealed,
        cards=cards,
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


@router.get("/match-progress")
def get_match_progress(
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
):
    """Returns how many completed matches the user has and how many until the next scratch card."""
    completed = _count_completed_matches(current_user.id, db)
    cards_earned = completed // MATCHES_PER_SCRATCH_CARD
    matches_in_current_cycle = completed % MATCHES_PER_SCRATCH_CARD
    matches_needed = MATCHES_PER_SCRATCH_CARD - matches_in_current_cycle
    return {
        "completed_matches": completed,
        "cards_earned": cards_earned,
        "matches_in_current_cycle": matches_in_current_cycle,
        "matches_needed_for_next_card": matches_needed,
        "matches_per_card": MATCHES_PER_SCRATCH_CARD,
    }


@router.get("/scratch-cards", response_model=ScratchCardDeckResponse)
def get_scratch_cards(
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
) -> Any:
    return _build_scratch_card_deck(current_user, db)



@router.post("/scratch-cards/{card_id}/reveal", response_model=ScratchCardRevealResponse)
def reveal_scratch_card(
    card_id: str,
    current_user: User = Depends(get_current_user_referral),
    db: Session = Depends(get_db),
) -> Any:
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    deck = _build_scratch_card_deck(user, db)
    card = next((entry for entry in deck.cards if entry.card_id == card_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail="Scratch card not found or expired")

    reference_id = _scratch_card_reference(user.id, card.card_id)
    existing_tx = (
        db.query(WalletTransaction)
        .filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.reference_id == reference_id,
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.transaction_type.in_(
                [SCRATCH_CARD_REWARD_TX_TYPE, SCRATCH_CARD_REVEAL_TX_TYPE]
            ),
        )
        .first()
    )

    if existing_tx:
        wallet_breakdown = get_wallet_breakdown(user)
        reward_amount = max(float(to_money(existing_tx.amount)), 0.0)
        return {
            "message": "Scratch card already revealed",
            "card_id": card.card_id,
            "reward_amount": reward_amount,
            "credited_to": "DEPOSIT" if reward_amount > 0 else "NONE",
            "wallet_balance": wallet_breakdown["balance"],
            "deposit_balance": wallet_breakdown["deposit_balance"],
            "winning_balance": wallet_breakdown["winning_balance"],
            "bonus_balance": wallet_breakdown["bonus_balance"],
        }

    reward_amount = to_money(card.reward_amount)
    credited_to = "NONE"
    tx_type = SCRATCH_CARD_REVEAL_TX_TYPE
    tx_amount = Decimal("0.00")

    if reward_amount > Decimal("0.00"):
        credit_wallet(user, reward_amount, WALLET_BUCKET_DEPOSIT)
        credited_to = "DEPOSIT"
        tx_type = SCRATCH_CARD_REWARD_TX_TYPE
        tx_amount = reward_amount

    db.add(
        WalletTransaction(
            user_id=user.id,
            amount=tx_amount,
            transaction_type=tx_type,
            status="SUCCESS",
            reference_id=reference_id,
            payment_mode="SCRATCH",
        )
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    wallet_breakdown = get_wallet_breakdown(user)
    return {
        "message": "Scratch card revealed",
        "card_id": card.card_id,
        "reward_amount": reward_amount,
        "credited_to": credited_to,
        "wallet_balance": wallet_breakdown["balance"],
        "deposit_balance": wallet_breakdown["deposit_balance"],
        "winning_balance": wallet_breakdown["winning_balance"],
        "bonus_balance": wallet_breakdown["bonus_balance"],
    }


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
