from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from models.config import SystemConfig
from models.user import User
from services.wallet_balances import to_money

DAILY_BONUS_USAGE_LIMIT_CONFIG_KEY = "daily_bonus_usage_limit_amount"
DEFAULT_DAILY_BONUS_USAGE_LIMIT_AMOUNT = Decimal("0.00")
BONUS_DAILY_RESET_MINUTE_IST = 1
IST = timezone(timedelta(hours=5, minutes=30))


def _current_bonus_cycle_ist() -> tuple[str, datetime, datetime]:
    now_ist = datetime.now(IST)
    reset_point_ist = now_ist.replace(
        hour=0,
        minute=BONUS_DAILY_RESET_MINUTE_IST,
        second=0,
        microsecond=0,
    )
    cycle_start_ist = reset_point_ist - timedelta(days=1) if now_ist < reset_point_ist else reset_point_ist
    cycle_end_ist = cycle_start_ist + timedelta(days=1)
    cycle_key = cycle_start_ist.date().isoformat()
    cycle_start_utc = cycle_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    cycle_end_utc = cycle_end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return cycle_key, cycle_start_utc, cycle_end_utc


def get_bonus_usage_cycle_key() -> str:
    cycle_key, _, _ = _current_bonus_cycle_ist()
    return cycle_key


def get_daily_bonus_limit_amount(db: Session) -> Decimal:
    config_row = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == DAILY_BONUS_USAGE_LIMIT_CONFIG_KEY)
        .first()
    )
    if not config_row or config_row.config_value is None:
        return to_money(DEFAULT_DAILY_BONUS_USAGE_LIMIT_AMOUNT)

    raw_value = str(config_row.config_value).strip()
    if not raw_value:
        return to_money(DEFAULT_DAILY_BONUS_USAGE_LIMIT_AMOUNT)

    try:
        parsed = to_money(raw_value)
    except Exception:
        return to_money(DEFAULT_DAILY_BONUS_USAGE_LIMIT_AMOUNT)

    if parsed < Decimal("0.00"):
        return to_money(DEFAULT_DAILY_BONUS_USAGE_LIMIT_AMOUNT)
    return parsed


def get_user_bonus_used_today(user: User, cycle_key: str) -> Decimal:
    stored_cycle = (getattr(user, "daily_bonus_cycle_key", None) or "").strip()
    if stored_cycle != cycle_key:
        return Decimal("0.00")
    return to_money(getattr(user, "daily_bonus_used", Decimal("0.00")) or Decimal("0.00"))


def get_daily_bonus_allowance(db: Session, user: User) -> tuple[str, Decimal, Decimal, Optional[Decimal]]:
    cycle_key = get_bonus_usage_cycle_key()
    used_today = get_user_bonus_used_today(user, cycle_key)
    limit_amount = get_daily_bonus_limit_amount(db)

    # 0 means unlimited daily bonus usage.
    if limit_amount <= Decimal("0.00"):
        return cycle_key, limit_amount, used_today, None

    remaining = to_money(limit_amount - used_today)
    if remaining < Decimal("0.00"):
        remaining = Decimal("0.00")
    return cycle_key, limit_amount, used_today, remaining


def register_bonus_usage(
    user: User,
    amount: Decimal | int | float | str,
    cycle_key: Optional[str] = None,
) -> Decimal:
    used_amount = to_money(amount)
    if used_amount <= Decimal("0.00"):
        return Decimal("0.00")

    active_cycle_key = cycle_key or get_bonus_usage_cycle_key()
    stored_cycle = (getattr(user, "daily_bonus_cycle_key", None) or "").strip()

    if stored_cycle != active_cycle_key:
        user.daily_bonus_cycle_key = active_cycle_key
        user.daily_bonus_used = Decimal("0.00")

    current_used = to_money(getattr(user, "daily_bonus_used", Decimal("0.00")) or Decimal("0.00"))
    user.daily_bonus_used = to_money(current_used + used_amount)
    return used_amount
