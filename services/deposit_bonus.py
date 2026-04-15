from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from models.config import SystemConfig
from models.user import User
from models.wallet import WalletTransaction
from schemas.admin import DepositBonusConfigUpdate
from services.wallet_balances import WALLET_BUCKET_BONUS, credit_wallet, to_money

logger = logging.getLogger("GamerzAdda.deposit_bonus")

DEPOSIT_BONUS_CONFIG_KEY = "deposit_bonus_rules"
DEPOSIT_BONUS_TX_TYPE = "DEPOSIT_BONUS"
_DEPOSIT_BONUS_REFERENCE_PREFIX = "DEPBONUS"
_ZERO = Decimal("0.00")


def _default_config() -> dict[str, Any]:
    return {"enabled": False, "rules": []}


def normalize_deposit_bonus_config(payload: dict[str, Any]) -> dict[str, Any]:
    parsed = DepositBonusConfigUpdate.model_validate(payload)

    normalized_rules: list[dict[str, Any]] = []
    for idx, rule in enumerate(parsed.rules, start=1):
        rule_dump = rule.model_dump()
        rule_id = str(rule_dump.get("id") or f"rule_{idx}").strip()[:64]
        if not rule_id:
            rule_id = f"rule_{idx}"

        label_raw = rule_dump.get("label")
        label = str(label_raw).strip()[:80] if label_raw else None

        min_amount = to_money(rule_dump["min_amount"])
        max_amount_raw = rule_dump.get("max_amount")
        max_amount = to_money(max_amount_raw) if max_amount_raw is not None else None

        bonus_value = to_money(rule_dump["bonus_value"])
        max_bonus_amount_raw = rule_dump.get("max_bonus_amount")
        max_bonus_amount = (
            to_money(max_bonus_amount_raw) if max_bonus_amount_raw is not None else None
        )

        normalized_rules.append(
            {
                "id": rule_id,
                "label": label,
                "min_amount": float(min_amount),
                "max_amount": float(max_amount) if max_amount is not None else None,
                "bonus_type": str(rule_dump["bonus_type"]).upper(),
                "bonus_value": float(bonus_value),
                "max_bonus_amount": (
                    float(max_bonus_amount) if max_bonus_amount is not None else None
                ),
                "is_active": bool(rule_dump.get("is_active", True)),
            }
        )

    normalized_rules.sort(
        key=lambda item: (
            Decimal(str(item["min_amount"])),
            Decimal(str(item["max_amount"])) if item["max_amount"] is not None else Decimal("999999999.99"),
            item["id"],
        )
    )

    return {
        "enabled": bool(parsed.enabled),
        "rules": normalized_rules,
    }


def get_deposit_bonus_config(db: Session) -> dict[str, Any]:
    record = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == DEPOSIT_BONUS_CONFIG_KEY)
        .first()
    )
    if not record or not (record.config_value or "").strip():
        return _default_config()

    try:
        raw_payload = json.loads(record.config_value)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON for %s. Falling back to defaults.", DEPOSIT_BONUS_CONFIG_KEY)
        return _default_config()

    try:
        return normalize_deposit_bonus_config(raw_payload)
    except Exception as exc:
        logger.warning("Invalid deposit bonus config payload: %s", exc)
        return _default_config()


def set_deposit_bonus_config(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_deposit_bonus_config(payload)
    serialized = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)

    record = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == DEPOSIT_BONUS_CONFIG_KEY)
        .first()
    )
    if not record:
        record = SystemConfig(
            config_key=DEPOSIT_BONUS_CONFIG_KEY,
            config_value=serialized,
            description="Configurable deposit bonus rules in JSON format",
        )
        db.add(record)
    else:
        record.config_value = serialized
        if not record.description:
            record.description = "Configurable deposit bonus rules in JSON format"

    return normalized


def _rule_bonus_amount(deposit_amount: Decimal, rule: dict[str, Any]) -> Decimal:
    bonus_type = str(rule.get("bonus_type") or "PERCENT").upper()
    bonus_value = to_money(rule.get("bonus_value"))

    if bonus_value <= _ZERO:
        return _ZERO

    if bonus_type == "PERCENT":
        base_bonus = (deposit_amount * bonus_value) / Decimal("100")
    else:
        base_bonus = bonus_value

    base_bonus = to_money(base_bonus)
    max_bonus_amount_raw = rule.get("max_bonus_amount")
    if max_bonus_amount_raw is not None:
        max_bonus_amount = to_money(max_bonus_amount_raw)
        if max_bonus_amount > _ZERO:
            base_bonus = min(base_bonus, max_bonus_amount)

    return to_money(base_bonus)


def evaluate_deposit_bonus(
    deposit_amount: Decimal | int | float | str,
    config_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, Decimal]:
    amount = to_money(deposit_amount)
    if amount <= _ZERO:
        return None, _ZERO

    if not bool(config_payload.get("enabled", False)):
        return None, _ZERO

    best_rule: dict[str, Any] | None = None
    best_bonus = _ZERO

    for rule in config_payload.get("rules", []):
        if not bool(rule.get("is_active", True)):
            continue

        min_amount = to_money(rule.get("min_amount"))
        max_amount_raw = rule.get("max_amount")
        max_amount = to_money(max_amount_raw) if max_amount_raw is not None else None

        if amount < min_amount:
            continue
        if max_amount is not None and amount > max_amount:
            continue

        candidate_bonus = _rule_bonus_amount(amount, rule)
        if candidate_bonus <= _ZERO:
            continue

        if candidate_bonus > best_bonus:
            best_bonus = candidate_bonus
            best_rule = rule
        elif candidate_bonus == best_bonus and best_rule is not None:
            # Prefer the more specific rule when bonus values tie.
            previous_min = to_money(best_rule.get("min_amount"))
            if min_amount > previous_min:
                best_rule = rule

    return best_rule, best_bonus


def apply_deposit_bonus_if_eligible(
    db: Session,
    user: User,
    deposit_tx: WalletTransaction,
    source: str,
) -> Decimal:
    if deposit_tx.transaction_type != "ADD_MONEY" or deposit_tx.status != "SUCCESS":
        return _ZERO

    deposit_amount = to_money(deposit_tx.amount)
    if deposit_amount <= _ZERO:
        return _ZERO

    bonus_reference_id = f"{_DEPOSIT_BONUS_REFERENCE_PREFIX}_{deposit_tx.id}"
    already_credited = (
        db.query(WalletTransaction.id)
        .filter(
            WalletTransaction.reference_id == bonus_reference_id,
            WalletTransaction.transaction_type == DEPOSIT_BONUS_TX_TYPE,
            WalletTransaction.status == "SUCCESS",
        )
        .first()
    )
    if already_credited:
        return _ZERO

    config_payload = get_deposit_bonus_config(db)
    matched_rule, bonus_amount = evaluate_deposit_bonus(deposit_amount, config_payload)
    if not matched_rule or bonus_amount <= _ZERO:
        return _ZERO

    credit_wallet(user, bonus_amount, WALLET_BUCKET_BONUS)

    rule_id = str(matched_rule.get("id") or "unknown")[:64]
    tx = WalletTransaction(
        user_id=user.id,
        amount=bonus_amount,
        transaction_type=DEPOSIT_BONUS_TX_TYPE,
        status="SUCCESS",
        reference_id=bonus_reference_id,
        payment_mode=(source or "SYSTEM")[:40],
        failure_reason=(
            f"SOURCE_ADD_MONEY_TX:{deposit_tx.id};"
            f"RULE:{rule_id};"
            f"BONUS_TYPE:{matched_rule.get('bonus_type')};"
            f"BASE_AMOUNT:{deposit_amount:.2f}"
        ),
    )

    db.add(user)
    db.add(tx)

    logger.info(
        "Deposit bonus credited: user=%s add_money_tx=%s bonus=%.2f rule=%s source=%s",
        user.id,
        deposit_tx.id,
        float(bonus_amount),
        rule_id,
        source,
    )

    return bonus_amount
