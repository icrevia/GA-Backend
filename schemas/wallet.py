from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import Optional


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
