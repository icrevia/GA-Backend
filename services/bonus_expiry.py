"""Bonus Expiry Service

Automatically expires bonus wallet transactions older than 30 days.
Each bonus transaction is expired individually — the exact credited amount
is deducted from the user's bonus_balance.

Designed to run as a periodic background task (~every 6 hours).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from models.user import User
from models.wallet import WalletTransaction
from services.notifications import add_user_notification
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    to_money,
    ensure_wallet_buckets,
    _bucket_value,
    _set_bucket_value,
    sync_wallet_total,
    ZERO_MONEY,
)

logger = logging.getLogger("GamerzAdda.bonus_expiry")

BONUS_VALIDITY_DAYS = 30
BONUS_EXPIRY_REMINDER_DAYS = 3

# Transaction types that credit bonus wallet and should be subject to expiry
EXPIRABLE_BONUS_TX_TYPES = frozenset({
    "REFERRAL_REWARD",
    "SIGNUP_BONUS",
    "PROMO_REWARD",
    "SPIN_REWARD",
})

BONUS_EXPIRED_TX_TYPE = "BONUS_EXPIRED"
BONUS_EXPIRY_REMINDER_TX_TYPE = "BONUS_EXPIRY_REMINDER"


def _debit_bonus_safe(user: User, amount: Decimal) -> Decimal:
    """Debit from bonus_balance, capped at available balance (never go negative)."""
    ensure_wallet_buckets(user)
    current = _bucket_value(user, WALLET_BUCKET_BONUS)
    deduct = min(to_money(amount), current)
    if deduct <= ZERO_MONEY:
        return ZERO_MONEY
    _set_bucket_value(user, WALLET_BUCKET_BONUS, current - deduct)
    sync_wallet_total(user)
    return deduct


def _source_label(tx_type: str) -> str:
    labels = {
        "REFERRAL_REWARD": "Referral Reward",
        "SIGNUP_BONUS": "Signup Bonus",
        "PROMO_REWARD": "Promo Reward",
        "SPIN_REWARD": "Spin Reward",
    }
    return labels.get(tx_type, "Bonus")


def process_expired_bonuses(db: Session) -> int:
    """Find and expire all bonus transactions older than 30 days.

    Returns the count of expired transactions processed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=BONUS_VALIDITY_DAYS)
    expired_count = 0

    # Find all eligible bonus transactions that are older than 30 days
    expired_txns = (
        db.query(WalletTransaction)
        .filter(
            WalletTransaction.transaction_type.in_(EXPIRABLE_BONUS_TX_TYPES),
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.amount > 0,
            WalletTransaction.created_at < cutoff,
        )
        .order_by(WalletTransaction.created_at.asc())
        .all()
    )

    for tx in expired_txns:
        # Check if this specific transaction was already expired
        already_expired = (
            db.query(WalletTransaction.id)
            .filter(
                WalletTransaction.transaction_type == BONUS_EXPIRED_TX_TYPE,
                WalletTransaction.user_id == tx.user_id,
                WalletTransaction.failure_reason.contains(f"EXPIRED_TX:{tx.id}"),
                WalletTransaction.status == "SUCCESS",
            )
            .first()
        )
        if already_expired:
            continue

        user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
        if not user:
            continue

        original_amount = to_money(tx.amount)
        deducted = _debit_bonus_safe(user, original_amount)

        if deducted <= ZERO_MONEY:
            # Bonus already spent — just mark as expired with 0 deduction
            pass

        source = _source_label(tx.transaction_type)

        expiry_tx = WalletTransaction(
            user_id=tx.user_id,
            amount=-deducted,  # negative to show deduction
            transaction_type=BONUS_EXPIRED_TX_TYPE,
            status="SUCCESS",
            reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
            payment_mode="SYSTEM",
            failure_reason=f"EXPIRED_TX:{tx.id}; {source} of ₹{original_amount:.2f} expired after {BONUS_VALIDITY_DAYS} days",
        )
        db.add(user)
        db.add(expiry_tx)

        if deducted > ZERO_MONEY:
            add_user_notification(
                db,
                tx.user_id,
                "Bonus Expired",
                (
                    f"Your ₹{deducted:.2f} {source.lower()} bonus has expired after "
                    f"{BONUS_VALIDITY_DAYS} days. Keep playing to earn more rewards!"
                ),
                "WALLET",
            )

        expired_count += 1

    if expired_count > 0:
        db.commit()

    return expired_count


def send_expiry_reminders(db: Session) -> int:
    """Send reminder notifications for bonuses expiring in the next 3 days.

    Returns the count of reminders sent.
    """
    now = datetime.now(timezone.utc)
    reminder_start = now - timedelta(days=BONUS_VALIDITY_DAYS - BONUS_EXPIRY_REMINDER_DAYS)
    reminder_end = now - timedelta(days=BONUS_VALIDITY_DAYS)

    # Bonuses that were created between (30 - 3) and 30 days ago
    # i.e., they will expire in the next 3 days
    expiring_txns = (
        db.query(WalletTransaction)
        .filter(
            WalletTransaction.transaction_type.in_(EXPIRABLE_BONUS_TX_TYPES),
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.amount > 0,
            WalletTransaction.created_at >= reminder_end,
            WalletTransaction.created_at <= reminder_start,
        )
        .all()
    )

    reminder_count = 0

    for tx in expiring_txns:
        # Check if already expired
        already_expired = (
            db.query(WalletTransaction.id)
            .filter(
                WalletTransaction.transaction_type == BONUS_EXPIRED_TX_TYPE,
                WalletTransaction.failure_reason.contains(f"EXPIRED_TX:{tx.id}"),
            )
            .first()
        )
        if already_expired:
            continue

        # Check if reminder already sent for this tx
        already_reminded = (
            db.query(WalletTransaction.id)
            .filter(
                WalletTransaction.transaction_type == BONUS_EXPIRY_REMINDER_TX_TYPE,
                WalletTransaction.failure_reason.contains(f"REMIND_TX:{tx.id}"),
            )
            .first()
        )
        if already_reminded:
            continue

        original_amount = to_money(tx.amount)
        source = _source_label(tx.transaction_type)
        expiry_date = tx.created_at + timedelta(days=BONUS_VALIDITY_DAYS)
        expiry_str = expiry_date.strftime("%d %b %Y")
        days_left = max(1, (expiry_date - now).days)

        # Record the reminder to avoid duplicate notifications
        reminder_tx = WalletTransaction(
            user_id=tx.user_id,
            amount=Decimal("0.00"),
            transaction_type=BONUS_EXPIRY_REMINDER_TX_TYPE,
            status="SUCCESS",
            reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
            payment_mode="SYSTEM",
            failure_reason=f"REMIND_TX:{tx.id}",
        )
        db.add(reminder_tx)

        add_user_notification(
            db,
            tx.user_id,
            "Bonus Expiring Soon! ⏰",
            (
                f"Your ₹{original_amount:.2f} {source.lower()} bonus expires in "
                f"{days_left} day{'s' if days_left != 1 else ''} (on {expiry_str}). "
                f"Use it before it's gone!"
            ),
            "WALLET",
        )

        reminder_count += 1

    if reminder_count > 0:
        db.commit()

    return reminder_count


def run_bonus_expiry_cycle(db: Session) -> dict:
    """Run a full expiry cycle: reminders first, then expiry processing.

    Returns a summary dict: {"expired": N, "reminders": N}
    """
    reminders = send_expiry_reminders(db)
    expired = process_expired_bonuses(db)

    if expired > 0 or reminders > 0:
        logger.info(
            "Bonus expiry cycle: expired=%d, reminders=%d",
            expired, reminders,
        )

    return {"expired": expired, "reminders": reminders}
