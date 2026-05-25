from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import List, Optional


class AddMoneyRequest(BaseModel):
    # Decimal keeps exact precision — no float rounding errors for money
    amount: Decimal


class Pay0InitResponse(BaseModel):
    payment_url: str
    order_id: str

class PaymentInitResponse(BaseModel):
    gateway: str
    pay0_init: Optional[Pay0InitResponse] = None


class DepositBonusOfferRule(BaseModel):
    id: str
    label: Optional[str] = None
    min_amount: Decimal
    max_amount: Optional[Decimal] = None
    bonus_type: str
    bonus_value: Decimal
    max_bonus_amount: Optional[Decimal] = None


class DepositBonusOffersResponse(BaseModel):
    enabled: bool
    rules: List[DepositBonusOfferRule]
    display_text: Optional[str] = None
    minimum_deposit_amount: Decimal


class DepositBonusPreviewResponse(BaseModel):
    eligible: bool
    amount: Decimal
    bonus_amount: Decimal
    message: str
    rule: Optional[DepositBonusOfferRule] = None


class CancelPaymentRequest(BaseModel):
    txnid: str


class WithdrawalRequest(BaseModel):
    amount: Decimal
    upi_id: str


class RejectWithdrawalRequest(BaseModel):
    reason: Optional[str] = None


class PromoRedeemRequest(BaseModel):
    code: str


class PromoRedeemResponse(BaseModel):
    message: str
    code: str
    reward_amount: Decimal
    wallet_balance: Decimal
    deposit_balance: Decimal
    winning_balance: Decimal
    bonus_balance: Decimal
    transaction_reference: str


class WalletTransactionResponse(BaseModel):
    id: int
    amount: Decimal
    transaction_type: str
    status: str
    reference_id: Optional[str] = None
    failure_reason: Optional[str] = None
    remark: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class WalletBalanceResponse(BaseModel):
    balance: Decimal
    deposit_balance: Decimal
    winning_balance: Decimal
    bonus_balance: Decimal
    withdrawable_balance: Decimal
    minimum_withdrawal_amount: Decimal
    daily_bonus_limit_amount: Decimal
    daily_bonus_used_today: Decimal
    daily_bonus_remaining_today: Optional[Decimal] = None
    daily_bonus_unlimited: bool = False


class SpinPlayResponse(BaseModel):
    message: str
    prize_amount: Decimal
    spin_cost: Decimal
    spins_used_today: int
    daily_spin_limit: int
    remaining_spins: int
    total_spins: int
    wallet_balance: Decimal
    deposit_balance: Decimal
    winning_balance: Decimal
    bonus_balance: Decimal
    daily_bonus_limit_amount: Decimal
    daily_bonus_used_today: Decimal
    daily_bonus_remaining_today: Optional[Decimal] = None
    daily_bonus_unlimited: bool = False
    daily_bonus_blocked_amount: Decimal = Decimal("0.00")


class WithdrawUpiAccountPayload(BaseModel):
    account_holder_name: str
    upi_id: str


class WithdrawUpiAccountListRequest(BaseModel):
    accounts: List[WithdrawUpiAccountPayload]
    selected_upi_id: Optional[str] = None


class WithdrawUpiAccountResponse(BaseModel):
    id: int
    account_holder_name: str
    upi_id: str

    class Config:
        from_attributes = True
