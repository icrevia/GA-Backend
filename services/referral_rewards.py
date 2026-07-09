from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.orm import Session

from models.config import SystemConfig
from models.user import User
from models.wallet import WalletTransaction
from services.notifications import add_user_notification
from services.wallet_balances import WALLET_BUCKET_BONUS, credit_wallet, to_money
from schemas.admin import ReferralRewardConfigUpdate

logger = logging.getLogger("GamerzAdda.referral_rewards")

REFERRAL_REWARD_CONFIG_KEY = "referral_reward_rules"

REFERRAL_REWARD_TX_TYPE = "REFERRAL_REWARD"
SIGNUP_BONUS_TX_TYPE = "SIGNUP_BONUS"
FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX = "REF_FIRST_DEPOSIT"

# Backward-compatible constants used by referral stats UI payload.
REFERRAL_LOW_BONUS_MIN = Decimal("0.00")
REFERRAL_LOW_BONUS_MAX = Decimal("0.00")
REFERRAL_HIGH_BONUS_MIN = Decimal("0.00")
REFERRAL_HIGH_BONUS_MAX = Decimal("0.00")
REFERRAL_JACKPOT_BONUS_MIN = Decimal("0.00")
REFERRAL_JACKPOT_BONUS_MAX = Decimal("0.00")
REFERRAL_LOW_BAND_PROBABILITY = 1.0
REFERRAL_HIGH_BAND_PROBABILITY = 0.0
REFERRAL_JACKPOT_BAND_PROBABILITY = 0.0
FIRST_DEPOSIT_MATCH_MULTIPLIER = Decimal("0.00")

BONUS_VALIDITY_DAYS = 30
_ZERO = Decimal("0.00")

_TRIGGER_SIGNUP = "REFERRAL_SIGNUP"
_TRIGGER_FIRST_DEPOSIT = "FIRST_SUCCESSFUL_DEPOSIT"


@dataclass
class ReferralRewardCreditResult:
    referrer_reward: Decimal = _ZERO
    referred_user_reward: Decimal = _ZERO
    rule_id: str = ""


def _to_money_decimal(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return _ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _expiry_label(days: int = BONUS_VALIDITY_DAYS) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    return expiry.strftime("%d %b %Y")


def _default_config() -> dict[str, Any]:
    return {"enabled": False, "rules": []}


def normalize_referral_reward_config(payload: dict[str, Any]) -> dict[str, Any]:
    parsed = ReferralRewardConfigUpdate.model_validate(payload)

    normalized_rules: list[dict[str, Any]] = []
    for idx, rule in enumerate(parsed.rules, start=1):
        rule_dump = rule.model_dump()
        rule_id = str(rule_dump.get("id") or f"rule_{idx}").strip()[:64]
        if not rule_id:
            rule_id = f"rule_{idx}"

        label_raw = rule_dump.get("label")
        label = str(label_raw).strip()[:80] if label_raw else None

        trigger = str(rule_dump.get("trigger") or _TRIGGER_SIGNUP).strip().upper()
        referred_user_reward = to_money(rule_dump.get("referred_user_reward"))
        referrer_reward = to_money(rule_dump.get("referrer_reward"))

        min_recharge_raw = rule_dump.get("min_recharge_amount")
        min_recharge_amount = (
            to_money(min_recharge_raw)
            if min_recharge_raw is not None
            else None
        )

        if trigger == _TRIGGER_SIGNUP:
            min_recharge_amount = None
        elif min_recharge_amount is None:
            min_recharge_amount = _ZERO

        normalized_rules.append(
            {
                "id": rule_id,
                "label": label,
                "trigger": trigger,
                "referred_user_reward": float(referred_user_reward),
                "referrer_reward": float(referrer_reward),
                "min_recharge_amount": (
                    float(min_recharge_amount) if min_recharge_amount is not None else None
                ),
                "max_reward_count_per_referrer": rule_dump.get("max_reward_count_per_referrer"),
                "is_active": bool(rule_dump.get("is_active", True)),
            }
        )

    normalized_rules.sort(key=lambda item: (item["trigger"], item["id"]))

    return {
        "enabled": bool(parsed.enabled),
        "rules": normalized_rules,
    }


def get_referral_reward_config(db: Session) -> dict[str, Any]:
    record = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == REFERRAL_REWARD_CONFIG_KEY)
        .first()
    )
    if not record or not (record.config_value or "").strip():
        return _default_config()

    try:
        raw_payload = json.loads(record.config_value)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON for %s. Falling back to defaults.", REFERRAL_REWARD_CONFIG_KEY)
        return _default_config()

    try:
        return normalize_referral_reward_config(raw_payload)
    except Exception as exc:
        logger.warning("Invalid referral reward config payload: %s", exc)
        return _default_config()


def set_referral_reward_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_referral_reward_config(payload)
    serialized = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)

    record = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == REFERRAL_REWARD_CONFIG_KEY)
        .first()
    )
    if not record:
        record = SystemConfig(
            config_key=REFERRAL_REWARD_CONFIG_KEY,
            config_value=serialized,
            description="Configurable referral reward rules in JSON format",
        )
        db.add(record)
    else:
        record.config_value = serialized
        if not record.description:
            record.description = "Configurable referral reward rules in JSON format"

    return normalized


def _first_deposit_bonus_reference_id(referred_user_id: int) -> str:
    return f"{FIRST_DEPOSIT_BONUS_REFERENCE_PREFIX}_{referred_user_id}"


def _has_signup_bonus_credit(db: Session, user_id: int) -> bool:
    exists = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.user_id == user_id,
            WalletTransaction.transaction_type == SIGNUP_BONUS_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
        )
        .first()
    )
    return exists is not None


def _has_first_deposit_referrer_reward(db: Session, referred_user_id: int) -> bool:
    reference_id = _first_deposit_bonus_reference_id(referred_user_id)
    exists = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.reference_id == reference_id,
            WalletTransaction.transaction_type == REFERRAL_REWARD_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
        )
        .first()
    )
    return exists is not None


def _count_referrer_rewards_for_rule(db: Session, referrer_user_id: int, rule_id: str) -> int:
    if not rule_id:
        return 0
    marker = f"RULE:{rule_id}"
    return (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.user_id == referrer_user_id,
            WalletTransaction.transaction_type == REFERRAL_REWARD_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.like(f"%{marker}%"),
        )
        .count()
    )


def _count_signup_rewards_for_rule(db: Session, referred_user_id: int, rule_id: str) -> int:
    if not rule_id:
        return 0
    marker = f"RULE:{rule_id}"
    return (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.user_id == referred_user_id,
            WalletTransaction.transaction_type == SIGNUP_BONUS_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.like(f"%{marker}%"),
        )
        .count()
    )


def _rule_applicable_for_signup(db: Session, rule: dict[str, Any], referrer_user_id: int, referred_user_id: int) -> bool:
    if not bool(rule.get("is_active", True)):
        return False

    if str(rule.get("trigger") or "").upper() != _TRIGGER_SIGNUP:
        return False

    if _count_signup_rewards_for_rule(db, referred_user_id, str(rule.get("id") or "")) > 0:
        return False

    max_count = rule.get("max_reward_count_per_referrer")
    if max_count is not None:
        already_count = _count_referrer_rewards_for_rule(db, referrer_user_id, str(rule.get("id") or ""))
        if already_count >= int(max_count):
            return False

    return True


def _rule_applicable_for_first_deposit(
    db: Session,
    rule: dict[str, Any],
    referrer_user_id: int,
    referred_user_id: int,
    deposit_amount: Decimal,
) -> bool:
    if not bool(rule.get("is_active", True)):
        return False

    if str(rule.get("trigger") or "").upper() != _TRIGGER_FIRST_DEPOSIT:
        return False

    min_recharge_raw = rule.get("min_recharge_amount")
    min_recharge = _to_money_decimal(min_recharge_raw)
    if deposit_amount < min_recharge:
        return False

    if _has_first_deposit_referrer_reward(db, referred_user_id):
        return False

    max_count = rule.get("max_reward_count_per_referrer")
    if max_count is not None:
        already_count = _count_referrer_rewards_for_rule(db, referrer_user_id, str(rule.get("id") or ""))
        if already_count >= int(max_count):
            return False

    return True


def _pick_signup_rule(db: Session, config: dict[str, Any], referrer_user_id: int, referred_user_id: int) -> dict[str, Any] | None:
    if not bool(config.get("enabled", False)):
        return None

    for rule in config.get("rules", []):
        if _rule_applicable_for_signup(db, rule, referrer_user_id, referred_user_id):
            return rule
    return None


def _pick_first_deposit_rule(
    db: Session,
    config: dict[str, Any],
    referrer_user_id: int,
    referred_user_id: int,
    deposit_amount: Decimal,
) -> dict[str, Any] | None:
    if not bool(config.get("enabled", False)):
        return None

    for rule in config.get("rules", []):
        if _rule_applicable_for_first_deposit(db, rule, referrer_user_id, referred_user_id, deposit_amount):
            return rule
    return None


def _credit_referral_side(
    db: Session,
    referrer: User,
    referred_user: User,
    amount: Decimal,
    rule_id: str,
    trigger: str,
) -> Decimal:
    if amount <= _ZERO:
        return _ZERO

    if trigger == _TRIGGER_FIRST_DEPOSIT:
        reference_id = _first_deposit_bonus_reference_id(referred_user.id)
    else:
        reference_id = f"REF_SIGNUP_{referred_user.id}_{rule_id}"

    existing = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.reference_id == reference_id,
            WalletTransaction.transaction_type == REFERRAL_REWARD_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.user_id == referrer.id,
        )
        .first()
    )
    if existing:
        return _ZERO

    credit_wallet(referrer, amount, WALLET_BUCKET_BONUS)
    tx = WalletTransaction(
        user_id=referrer.id,
        amount=amount,
        transaction_type=REFERRAL_REWARD_TX_TYPE,
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode="REFERRAL",
        failure_reason=f"SOURCE_USER:{referred_user.id};RULE:{rule_id};TRIGGER:{trigger}",
    )
    db.add(referrer)
    db.add(tx)

    add_user_notification(
        db,
        referrer.id,
        "Referral Bonus Credited! 🎉",
        (
            f"{referred_user.username} completed referral condition. "
            f"₹{amount:.2f} bonus credited to your wallet. "
            f"Valid for {BONUS_VALIDITY_DAYS} days (expires {_expiry_label()})."
        ),
        "REFERRAL",
    )

    return amount


def _credit_referred_user_side(
    db: Session,
    referred_user: User,
    amount: Decimal,
    rule_id: str,
    trigger: str,
) -> Decimal:
    if amount <= _ZERO:
        return _ZERO

    reference_id = f"SIGNUP_REWARD_{referred_user.id}_{rule_id}_{trigger}"
    existing = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.reference_id == reference_id,
            WalletTransaction.transaction_type == SIGNUP_BONUS_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.user_id == referred_user.id,
        )
        .first()
    )
    if existing:
        return _ZERO

    credit_wallet(referred_user, amount, WALLET_BUCKET_BONUS)
    tx = WalletTransaction(
        user_id=referred_user.id,
        amount=amount,
        transaction_type=SIGNUP_BONUS_TX_TYPE,
        status="SUCCESS",
        reference_id=reference_id,
        payment_mode="REFERRAL",
        failure_reason=f"RULE:{rule_id};TRIGGER:{trigger}",
    )
    db.add(referred_user)
    db.add(tx)

    add_user_notification(
        db,
        referred_user.id,
        "Referral Welcome Reward 🎁",
        (
            f"₹{amount:.2f} bonus has been added to your wallet via referral. "
            f"Valid for {BONUS_VALIDITY_DAYS} days (expires {_expiry_label()})."
        ),
        "WALLET",
    )

    return amount


def generate_weighted_referral_bonus() -> Decimal:
    # Legacy compatibility helper.
    return _ZERO


def generate_signup_bonus() -> Decimal:
    # Legacy compatibility helper.
    return _ZERO


def credit_signup_bonus(db: Session, new_user: User) -> Decimal | None:
    if not new_user.referred_by_id:
        return None

    if _has_signup_bonus_credit(db, new_user.id):
        return None

    referrer = db.query(User).filter(User.id == new_user.referred_by_id).with_for_update().first()
    if not referrer or not bool(referrer.is_active):
        return None

    config = get_referral_reward_config(db)
    rule = _pick_signup_rule(db, config, referrer_user_id=referrer.id, referred_user_id=new_user.id)
    if not rule:
        return None

    rule_id = str(rule.get("id") or "rule")
    trigger = str(rule.get("trigger") or _TRIGGER_SIGNUP)

    referred_user_reward = _to_money_decimal(rule.get("referred_user_reward"))
    referrer_reward = _to_money_decimal(rule.get("referrer_reward"))

    credited_to_referred = _credit_referred_user_side(
        db=db,
        referred_user=new_user,
        amount=referred_user_reward,
        rule_id=rule_id,
        trigger=trigger,
    )
    _credit_referral_side(
        db=db,
        referrer=referrer,
        referred_user=new_user,
        amount=referrer_reward,
        rule_id=rule_id,
        trigger=trigger,
    )

    if credited_to_referred <= _ZERO:
        return None
    return credited_to_referred


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

    referrer = db.query(User).filter(User.id == referred_user.referred_by_id).with_for_update().first()
    if not referrer or not bool(referrer.is_active):
        return None

    config = get_referral_reward_config(db)
    rule = _pick_first_deposit_rule(
        db=db,
        config=config,
        referrer_user_id=referrer.id,
        referred_user_id=referred_user.id,
        deposit_amount=to_money(deposit_tx.amount),
    )
    if not rule:
        return None

    rule_id = str(rule.get("id") or "rule")
    trigger = str(rule.get("trigger") or _TRIGGER_FIRST_DEPOSIT)

    referrer_reward = _to_money_decimal(rule.get("referrer_reward"))
    referred_user_reward = _to_money_decimal(rule.get("referred_user_reward"))

    credited_to_referrer = _credit_referral_side(
        db=db,
        referrer=referrer,
        referred_user=referred_user,
        amount=referrer_reward,
        rule_id=rule_id,
        trigger=trigger,
    )

    _credit_referred_user_side(
        db=db,
        referred_user=referred_user,
        amount=referred_user_reward,
        rule_id=rule_id,
        trigger=trigger,
    )

    if credited_to_referrer <= _ZERO:
        return None
    return credited_to_referrer
