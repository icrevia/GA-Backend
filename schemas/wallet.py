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


class WithdrawalRequest(BaseModel):
    amount: Decimal
    upi_id: str


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
    created_at: datetime

    class Config:
        from_attributes = True


class WalletBalanceResponse(BaseModel):
    balance: Decimal
    deposit_balance: Decimal
    winning_balance: Decimal
    bonus_balance: Decimal
    withdrawable_balance: Decimal


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
