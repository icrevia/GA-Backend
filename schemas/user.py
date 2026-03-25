from pydantic import BaseModel, EmailStr
from typing import Optional

class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
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
    email: Optional[str] = None
    upi_id: Optional[str] = None
    bgmi_id: Optional[str] = None
    valorant_id: Optional[str] = None
    freefire_id: Optional[str] = None
