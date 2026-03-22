from pydantic import BaseModel, EmailStr
from typing import Optional

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    email: EmailStr
    role: str
    wallet_balance: float
    upi_id: Optional[str] = None
    bgmi_id: Optional[str] = None
    valorant_id: Optional[str] = None
    freefire_id: Optional[str] = None
    
    class Config:
        from_attributes = True

class UserUpdate(BaseModel):
    username: Optional[str] = None
    upi_id: Optional[str] = None
    bgmi_id: Optional[str] = None
    valorant_id: Optional[str] = None
    freefire_id: Optional[str] = None
