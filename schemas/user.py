from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional, List
import re
from datetime import datetime


class UserCreate(BaseModel):
    # Allows: letters, digits, spaces, dots, underscores, hyphens (full names like "Rahul Mondalaa")
    # Blocks: XSS chars (< > " ' / \ & ; `), SQL injection chars
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_ .\-]+$")
    email: EmailStr
    phone_number: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")
    referral_code: Optional[str] = None


    @field_validator("username")
    @classmethod
    def username_no_spaces(cls, v: str) -> str:
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_must_be_gmail(cls, v: str) -> str:
        if not v.lower().endswith("@gmail.com"):
            raise ValueError("Only Gmail addresses are accepted. Please use a @gmail.com email.")
        return v.lower()


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)   # RFC 5321 max email length
    password: Optional[str] = Field(None, max_length=128)
    browser_geo_latitude: Optional[float] = Field(None, ge=-90, le=90)
    browser_geo_longitude: Optional[float] = Field(None, ge=-180, le=180)
    browser_geo_accuracy_m: Optional[float] = Field(None, ge=0)
    browser_geo_captured_at: Optional[str] = Field(None, max_length=64)
    browser_geo_provider: Optional[str] = Field(None, max_length=40)
    browser_geo_permission: Optional[str] = Field(None, max_length=24)



class UserRestrictionView(BaseModel):
    id: int
    scope: str
    page_key: Optional[str] = None
    reason: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    phone_number: Optional[str] = None
    role: str
    wallet_balance: float
    deposit_balance: float = 0.0
    winning_balance: float = 0.0
    bonus_balance: float = 0.0
    profile_pic: Optional[str] = None
    bio: Optional[str] = None
    upi_id: Optional[str] = None
    freefire_id: Optional[str] = None
    is_active: bool = True
    admin_permissions: Optional[str] = None
    active_restrictions: List[UserRestrictionView] = Field(default_factory=list)
    last_login_ip: Optional[str] = None

    # Path to stored face image (if enrolled)
    face_image_path: Optional[str] = None

    class Config:
        from_attributes = True


class FullProfileResponse(BaseModel):
    user: UserResponse
    stats: dict
    balance_details: dict


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=32)
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    bio: Optional[str] = Field(None, max_length=30)
    upi_id: Optional[str] = Field(None, max_length=100)
    freefire_id: Optional[str] = Field(None, max_length=50)


class SecureContactUpdate(BaseModel):
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")

    @model_validator(mode="after")
    def validate_target_fields(self):
        if self.email is None and self.phone_number is None:
            raise ValueError("Provide at least one field: email or phone_number")
        return self


class UserRestrictionView(BaseModel):
    id: int
    scope: str
    page_key: Optional[str] = None
    reason: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

class SubAdminCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_ .\-]+$")
    email: EmailStr
    phone_number: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")
    password: str = Field(..., min_length=6, max_length=128)
    admin_permissions: Optional[str] = None

class SubAdminUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_ .\-]+$")
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=6, max_length=128)
    admin_permissions: Optional[str] = None
