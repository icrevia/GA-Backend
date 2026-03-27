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

class RazorpayInitResponse(BaseModel):
    order_id: str
    amount: int  # in paise
    currency: str = "INR"
    key_id: str
    description: str
    prefill_name: str
    prefill_email: str
    prefill_contact: str
    txnid: str

class PaymentInitResponse(BaseModel):
    gateway: str  # "PAYU" or "RAZORPAY"
    payu_init: Optional[PayUInitResponse] = None
    razorpay_init: Optional[RazorpayInitResponse] = None


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
