from __future__ import annotations

import random
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from models.user import User
from models.wallet import WalletTransaction
from services.notifications import add_user_notification

REFERRAL_LOW_BONUS_MIN = Decimal("50.00")
REFERRAL_LOW_BONUS_MAX = Decimal("60.00")
REFERRAL_HIGH_BONUS_MIN = Decimal("70.00")
REFERRAL_HIGH_BONUS_MAX = Decimal("80.00")
REFERRAL_JACKPOT_BONUS_MIN = Decimal("90.00")
REFERRAL_JACKPOT_BONUS_MAX = Decimal("100.00")
REFERRAL_LOW_BAND_PROBABILITY = 0.90
REFERRAL_HIGH_BAND_PROBABILITY = 0.08
REFERRAL_JACKPOT_BAND_PROBABILITY = 0.02

FIRST_DEPOSIT_MATCH_MULTIPLIER = Decimal("1.00")
FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX = "REF_FIRST_DEPOSIT"
REFERRAL_REWARD_TX_TYPE = "REFERRAL_REWARD"

_rng = random.SystemRandom()


def _to_money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _random_money(min_amount: Decimal, max_amount: Decimal) -> Decimal:
    min_cents = int((min_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    max_cents = int((max_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sampled_cents = _rng.randint(min_cents, max_cents)
    return _to_money(Decimal(sampled_cents) / Decimal("100"))


def generate_weighted_referral_bonus() -> Decimal:
    roll = _rng.random()
    if roll < REFERRAL_LOW_BAND_PROBABILITY:
        return _random_money(REFERRAL_LOW_BONUS_MIN, REFERRAL_LOW_BONUS_MAX)
    if roll < REFERRAL_LOW_BAND_PROBABILITY + REFERRAL_HIGH_BAND_PROBABILITY:
        return _random_money(REFERRAL_HIGH_BONUS_MIN, REFERRAL_HIGH_BONUS_MAX)
    return _random_money(REFERRAL_JACKPOT_BONUS_MIN, REFERRAL_JACKPOT_BONUS_MAX)


def _first_deposit_bonus_reference_id(referred_user_id: int) -> str:
    return f"{FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX}_{referred_user_id}"


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

    first_deposit_amount = _to_money(deposit_tx.amount or Decimal("0"))
    if first_deposit_amount <= Decimal("0"):
        return None

    weighted_bonus = generate_weighted_referral_bonus()
    first_deposit_match_bonus = _to_money(first_deposit_amount * FIRST_DEPOSIT_MATCH_MULTIPLIER)
    total_bonus = _to_money(weighted_bonus + first_deposit_match_bonus)

    current_balance = _to_money(referrer.wallet_balance or Decimal("0"))
    referrer.wallet_balance = _to_money(current_balance + total_bonus)

    reward_tx = WalletTransaction(
        user_id=referrer.id,
        amount=total_bonus,
        transaction_type=REFERRAL_REWARD_TX_TYPE,
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode=deposit_tx.payment_mode or "REFERRAL",
    )

    db.add(referrer)
    db.add(reward_tx)

    add_user_notification(
        db,
        referrer.id,
        "Referral Bonus Credited",
        (
            f"{referred_user.username} completed first deposit of ₹{first_deposit_amount:.2f}. "
            f"Deposit match + weighted reward credited: ₹{total_bonus:.2f}."
        ),
        "REFERRAL",
    )

    return total_bonus
