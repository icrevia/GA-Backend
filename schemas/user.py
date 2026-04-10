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


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)   # RFC 5321 max email length
    browser_geo_latitude: Optional[float] = Field(None, ge=-90, le=90)
    browser_geo_longitude: Optional[float] = Field(None, ge=-180, le=180)
    browser_geo_accuracy_m: Optional[float] = Field(None, ge=0, le=100000)
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
    upi_id: Optional[str] = None
    profile_pic: Optional[str] = None
    bio: Optional[str] = None
    bgmi_id: Optional[str] = None
    valorant_id: Optional[str] = None
    freefire_id: Optional[str] = None
    is_active: bool = True
    active_restrictions: List[UserRestrictionView] = Field(default_factory=list)

    # Path to stored face image (if enrolled)
    face_image_path: Optional[str] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=32)
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    upi_id: Optional[str] = Field(None, max_length=50)
    bio: Optional[str] = Field(None, max_length=30)
    bgmi_id: Optional[str] = Field(None, max_length=50)
    valorant_id: Optional[str] = Field(None, max_length=50)
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



