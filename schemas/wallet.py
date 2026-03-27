from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import Optional


class AddMoneyRequest(BaseModel):
    # Decimal keeps exact precision — no float rounding errors for money
    amount: Decimal


class PayUInitResponse(BaseModel):
    txnid: str
    amount: Decimal
    productinfo: str
    firstname: str
    email: str
    phone: str
    surl: str
    furl: str
    hash: str
    key: str
    action: str


class WithdrawalRequest(BaseModel):
    amount: Decimal
    upi_id: str


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
