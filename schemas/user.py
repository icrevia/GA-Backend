from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional
import re


class UserCreate(BaseModel):
    # Allows: letters, digits, spaces, dots, underscores, hyphens (full names like "Rahul Mondalaa")
    # Blocks: XSS chars (< > " ' / \ & ; `), SQL injection chars
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_ .\-]+$")
    email: EmailStr
    phone_number: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")
    password: str = Field(..., min_length=6, max_length=128)
    referral_code: Optional[str] = None

    @field_validator("username")
    @classmethod
    def username_no_spaces(cls, v: str) -> str:
        return v.strip()


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)   # RFC 5321 max email length
    password: str = Field(..., max_length=128) # Prevent bcrypt CPU-spike DoS


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    phone_number: Optional[str] = None
    role: str
    wallet_balance: float
    upi_id: Optional[str] = None
    profile_pic: Optional[str] = None
    bgmi_id: Optional[str] = None
    valorant_id: Optional[str] = None
    freefire_id: Optional[str] = None
    is_active: bool = True

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=32)
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    upi_id: Optional[str] = Field(None, max_length=50)
    bgmi_id: Optional[str] = Field(None, max_length=50)
    valorant_id: Optional[str] = Field(None, max_length=50)
    freefire_id: Optional[str] = Field(None, max_length=50)


class SecureContactUpdate(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")

    @model_validator(mode="after")
    def validate_target_fields(self):
        if self.email is None and self.phone_number is None:
            raise ValueError("Provide at least one field: email or phone_number")
        return self


class PasswordChangeRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)
