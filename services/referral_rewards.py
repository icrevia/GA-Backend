from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from models.user import User
from models.wallet import WalletTransaction
from services.notifications import add_user_notification
from services.wallet_balances import WALLET_BUCKET_BONUS, credit_wallet

# ── Reward ranges ──────────────────────────────────────────────
REFERRER_REWARD_MIN = Decimal("40.00")
REFERRER_REWARD_MAX = Decimal("50.00")

SIGNUP_BONUS_MIN = Decimal("20.00")
SIGNUP_BONUS_MAX = Decimal("30.00")

BONUS_VALIDITY_DAYS = 30

# Legacy constants (kept for backward-compat with referral.py API response)
REFERRAL_LOW_BONUS_MIN = REFERRER_REWARD_MIN
REFERRAL_LOW_BONUS_MAX = REFERRER_REWARD_MAX
REFERRAL_HIGH_BONUS_MIN = REFERRER_REWARD_MIN
REFERRAL_HIGH_BONUS_MAX = REFERRER_REWARD_MAX
REFERRAL_JACKPOT_BONUS_MIN = REFERRER_REWARD_MIN
REFERRAL_JACKPOT_BONUS_MAX = REFERRER_REWARD_MAX
REFERRAL_LOW_BAND_PROBABILITY = 1.0
REFERRAL_HIGH_BAND_PROBABILITY = 0.0
REFERRAL_JACKPOT_BAND_PROBABILITY = 0.0
FIRST_DEPOSIT_MATCH_MULTIPLIER = Decimal("0.00")  # removed

FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX = "REF_FIRST_DEPOSIT"
REFERRAL_REWARD_TX_TYPE = "REFERRAL_REWARD"
SIGNUP_BONUS_TX_TYPE = "SIGNUP_BONUS"

_rng = random.SystemRandom()


def _to_money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _random_money(min_amount: Decimal, max_amount: Decimal) -> Decimal:
    min_cents = int((min_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    max_cents = int((max_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sampled_cents = _rng.randint(min_cents, max_cents)
    return _to_money(Decimal(sampled_cents) / Decimal("100"))


def _expiry_date_from_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=BONUS_VALIDITY_DAYS)


def _expiry_label(days: int = BONUS_VALIDITY_DAYS) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    return expiry.strftime("%d %b %Y")


def generate_weighted_referral_bonus() -> Decimal:
    """Flat random between REFERRER_REWARD_MIN and REFERRER_REWARD_MAX."""
    return _random_money(REFERRER_REWARD_MIN, REFERRER_REWARD_MAX)


def generate_signup_bonus() -> Decimal:
    """Flat random between SIGNUP_BONUS_MIN and SIGNUP_BONUS_MAX."""
    return _random_money(SIGNUP_BONUS_MIN, SIGNUP_BONUS_MAX)


def _first_deposit_bonus_reference_id(referred_user_id: int) -> str:
    return f"{FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX}_{referred_user_id}"


# ── Signup Bonus for New Referred User ────────────────────────
def credit_signup_bonus(db: Session, new_user: User) -> Decimal | None:
    """Credit instant signup bonus to new user who joined via referral.
    Returns the bonus amount or None if not applicable.
    """
    if not new_user.referred_by_id:
        return None

    # Dedup: check if already credited
    existing = db.query(WalletTransaction.id).filter(
        WalletTransaction.user_id == new_user.id,
        WalletTransaction.transaction_type == SIGNUP_BONUS_TX_TYPE,
        WalletTransaction.status == "SUCCESS",
    ).first()
    if existing:
        return None

    bonus = generate_signup_bonus()
    credit_wallet(new_user, bonus, WALLET_BUCKET_BONUS)

    tx = WalletTransaction(
        user_id=new_user.id,
        amount=bonus,
        transaction_type=SIGNUP_BONUS_TX_TYPE,
        status="SUCCESS",
        reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
        payment_mode="REFERRAL",
    )
    db.add(new_user)
    db.add(tx)

    add_user_notification(
        db,
        new_user.id,
        "Welcome Bonus Credited! 🎉",
        (
            f"₹{bonus:.2f} bonus has been added to your wallet as a welcome gift! "
            f"Valid for {BONUS_VALIDITY_DAYS} days (expires {_expiry_label()}). "
            f"Start playing tournaments to win more!"
        ),
        "WALLET",
    )

    return bonus


# ── Referrer Reward on First Deposit ──────────────────────────
def maybe_credit_referrer_for_first_successful_deposit(
    db: Session,
    referred_user: User,
    deposit_tx: WalletTransaction,
) -> Decimal | None:
    if not referred_user.referred_by_id:
        return None

    if deposit_tx.user_id != referred_user.id:
        return None

    if deposit_tx.transaction_type != "ADD_MONEY" or deposit_tx.status != "SUCCESS":
        return None

    existing_successful_deposit = db.query(WalletTransaction.id).filter(
        WalletTransaction.user_id == referred_user.id,
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.id != deposit_tx.id,
    ).first()
    if existing_successful_deposit:
        return None

    reference_id = _first_deposit_bonus_reference_id(referred_user.id)
    already_credited = db.query(WalletTransaction.id).filter(
        WalletTransaction.reference_id == reference_id,
        WalletTransaction.status == "SUCCESS",
    ).first()
    if already_credited:
        return None

    referrer = db.query(User).filter(User.id == referred_user.referred_by_id).with_for_update().first()
    if not referrer or not bool(referrer.is_active):
        return None

    total_bonus = generate_weighted_referral_bonus()
    credit_wallet(referrer, total_bonus, WALLET_BUCKET_BONUS)

    reward_tx = WalletTransaction(
        user_id=referrer.id,
        amount=total_bonus,
        transaction_type=REFERRAL_REWARD_TX_TYPE,
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode="REFERRAL",
    )

    db.add(referrer)
    db.add(reward_tx)

    add_user_notification(
        db,
        referrer.id,
        "Referral Bonus Credited! 🎉",
        (
            f"{referred_user.username} completed their first deposit! "
            f"₹{total_bonus:.2f} bonus credited to your wallet. "
            f"Valid for {BONUS_VALIDITY_DAYS} days (expires {_expiry_label()})."
        ),
        "REFERRAL",
    )

    return total_bonus
