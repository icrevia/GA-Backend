from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from models.user import User

MONEY_QUANT = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")

WALLET_BUCKET_DEPOSIT = "deposit"
WALLET_BUCKET_WINNING = "winning"
WALLET_BUCKET_BONUS = "bonus"

_ALL_BUCKETS = (
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    WALLET_BUCKET_BONUS,
)


class InsufficientWalletBalanceError(ValueError):
    def __init__(self, required: Decimal, available: Decimal):
        self.required = to_money(required)
        self.available = to_money(available)
        super().__init__(
            f"Insufficient wallet balance. Required={self.required:.2f}, available={self.available:.2f}"
        )


def to_money(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO_MONEY
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _bucket_attr(bucket: str) -> str:
    mapping = {
        WALLET_BUCKET_DEPOSIT: "deposit_balance",
        WALLET_BUCKET_WINNING: "winning_balance",
        WALLET_BUCKET_BONUS: "bonus_balance",
    }
    if bucket not in mapping:
        raise ValueError(f"Unsupported wallet bucket: {bucket}")
    return mapping[bucket]


def _bucket_value(user: User, bucket: str) -> Decimal:
    return to_money(getattr(user, _bucket_attr(bucket), ZERO_MONEY))


def _set_bucket_value(user: User, bucket: str, value: Decimal) -> None:
    setattr(user, _bucket_attr(bucket), to_money(value))


def sync_wallet_total(user: User) -> Decimal:
    total = (
        _bucket_value(user, WALLET_BUCKET_DEPOSIT)
        + _bucket_value(user, WALLET_BUCKET_WINNING)
        + _bucket_value(user, WALLET_BUCKET_BONUS)
    )
    user.wallet_balance = to_money(total)
    return user.wallet_balance


def ensure_wallet_buckets(user: User) -> None:
    deposit = _bucket_value(user, WALLET_BUCKET_DEPOSIT)
    winning = _bucket_value(user, WALLET_BUCKET_WINNING)
    bonus = _bucket_value(user, WALLET_BUCKET_BONUS)

    # Backward compatibility for pre-bucket users: preserve legacy total in deposit bucket.
    if legacy_total > ZERO_MONEY and deposit == ZERO_MONEY and winning == ZERO_MONEY and bonus == ZERO_MONEY:
        deposit = legacy_total

    changes = False
    if _bucket_value(user, WALLET_BUCKET_DEPOSIT) != deposit:
        _set_bucket_value(user, WALLET_BUCKET_DEPOSIT, deposit)
        changes = True
    if _bucket_value(user, WALLET_BUCKET_WINNING) != winning:
        _set_bucket_value(user, WALLET_BUCKET_WINNING, winning)
        changes = True
    if _bucket_value(user, WALLET_BUCKET_BONUS) != bonus:
        _set_bucket_value(user, WALLET_BUCKET_BONUS, bonus)
        changes = True

    if changes or to_money(getattr(user, "wallet_balance", ZERO_MONEY)) != (deposit + winning + bonus):
        sync_wallet_total(user)


def get_wallet_breakdown(user: User) -> dict[str, Decimal]:
    ensure_wallet_buckets(user)
    deposit = _bucket_value(user, WALLET_BUCKET_DEPOSIT)
    winning = _bucket_value(user, WALLET_BUCKET_WINNING)
    bonus = _bucket_value(user, WALLET_BUCKET_BONUS)
    total = to_money(user.wallet_balance)
    # Business rule: only winnings are withdrawable.
    withdrawable = to_money(winning)
    return {
        "balance": total,
        "deposit_balance": deposit,
        "winning_balance": winning,
        "bonus_balance": bonus,
        "withdrawable_balance": withdrawable,
    }


def get_total_balance(user: User) -> Decimal:
    ensure_wallet_buckets(user)
    return to_money(user.wallet_balance)


def get_withdrawable_balance(user: User) -> Decimal:
    ensure_wallet_buckets(user)
    return to_money(_bucket_value(user, WALLET_BUCKET_WINNING))


def credit_wallet(user: User, amount: Decimal | int | float | str, bucket: str) -> Decimal:
    ensure_wallet_buckets(user)
    credit_amount = to_money(amount)
    if credit_amount <= ZERO_MONEY:
        raise ValueError("Credit amount must be positive")

    current = _bucket_value(user, bucket)
    _set_bucket_value(user, bucket, current + credit_amount)
    sync_wallet_total(user)
    return credit_amount


def debit_wallet(
    user: User,
    amount: Decimal | int | float | str,
    spend_order: Iterable[str],
) -> dict[str, Decimal]:
    ensure_wallet_buckets(user)
    debit_amount = to_money(amount)
    if debit_amount <= ZERO_MONEY:
        raise ValueError("Debit amount must be positive")

    ordered_buckets: list[str] = []
    for bucket in spend_order:
        if bucket not in _ALL_BUCKETS:
            raise ValueError(f"Unsupported wallet bucket in spend order: {bucket}")
        if bucket not in ordered_buckets:
            ordered_buckets.append(bucket)

    available = sum((_bucket_value(user, bucket) for bucket in ordered_buckets), ZERO_MONEY)
    if available < debit_amount:
        raise InsufficientWalletBalanceError(required=debit_amount, available=available)

    deductions = {bucket: ZERO_MONEY for bucket in _ALL_BUCKETS}
    remaining = debit_amount

    for bucket in ordered_buckets:
        if remaining <= ZERO_MONEY:
            break
        current = _bucket_value(user, bucket)
        if current <= ZERO_MONEY:
            continue
        take = min(current, remaining)
        _set_bucket_value(user, bucket, current - take)
        deductions[bucket] = to_money(deductions[bucket] + take)
        remaining = to_money(remaining - take)

    sync_wallet_total(user)
    return deductions
