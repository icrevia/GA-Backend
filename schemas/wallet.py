from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class AddMoneyRequest(BaseModel):
    amount: float

class PayUInitResponse(BaseModel):
    txnid: str
    amount: float
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
    amount: float
    upi_id: str

class WalletTransactionResponse(BaseModel):
    id: int
    amount: float
    transaction_type: str
    status: str
    reference_id: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True

class WalletBalanceResponse(BaseModel):
    balance: float
