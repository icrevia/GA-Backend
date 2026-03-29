from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from api.deps import get_current_user, get_current_active_admin
from core.database import get_db
from core.security import hash_password, verify_password
from models.user import User
from schemas.user import UserResponse, UserUpdate, SecureContactUpdate, PasswordChangeRequest

router = APIRouter()


def _normalize_phone(phone_number: str) -> str:
    normalized = phone_number.strip().replace(" ", "")
    if len(normalized) == 10 and normalized.isdigit():
        normalized = f"+91{normalized}"
    return normalized

@router.get("/me", response_model=UserResponse)
def read_user_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.put("/me", response_model=UserResponse)
def update_user_me(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if user_update.username is not None:
        current_user.username = user_update.username

    # Enforce password-gated flow for email/phone updates.
    if user_update.email is not None:
        normalized_email = str(user_update.email).strip().lower()
        current_email = (current_user.email or "").strip().lower()
        if normalized_email != current_email:
            raise HTTPException(
                status_code=400,
                detail="Use Account Safety to change email. Password verification is required.",
            )

    if user_update.phone_number is not None:
        normalized_phone = _normalize_phone(user_update.phone_number)
        current_phone = (current_user.phone_number or "").strip()
        if normalized_phone != current_phone:
            raise HTTPException(
                status_code=400,
                detail="Use Account Safety to change phone number. Password verification is required.",
            )

    if user_update.upi_id is not None:
        current_user.upi_id = user_update.upi_id
    if user_update.bgmi_id is not None:
        current_user.bgmi_id = user_update.bgmi_id
    if user_update.valorant_id is not None:
        current_user.valorant_id = user_update.valorant_id
    if user_update.freefire_id is not None:
        current_user.freefire_id = user_update.freefire_id
        
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/security/contact", response_model=UserResponse)
def secure_update_contact(
    payload: SecureContactUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.hashed_password or not verify_password(payload.password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid password")

    changed = False

    if payload.email is not None:
        new_email = str(payload.email).strip().lower()
        current_email = (current_user.email or "").strip().lower()
        if new_email != current_email:
            existing_email_user = db.query(User).filter(
                User.email == new_email,
                User.id != current_user.id,
            ).first()
            if existing_email_user:
                raise HTTPException(status_code=400, detail="Email already registered")
            current_user.email = new_email
            changed = True

    if payload.phone_number is not None:
        new_phone = _normalize_phone(payload.phone_number)
        current_phone = (current_user.phone_number or "").strip()
        if new_phone != current_phone:
            existing_phone_user = db.query(User).filter(
                User.phone_number == new_phone,
                User.id != current_user.id,
            ).first()
            if existing_phone_user:
                raise HTTPException(status_code=400, detail="Phone number already in use")
            current_user.phone_number = new_phone
            changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="No contact changes detected")

    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/security/password")
def secure_change_password(
    payload: PasswordChangeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_password = payload.new_password.strip()
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    if current_user.hashed_password and verify_password(new_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different from current password")

    current_user.hashed_password = hash_password(new_password)
    db.add(current_user)
    db.commit()

    return {"message": "Password updated successfully"}

@router.get("/", response_model=List[UserResponse])
def read_users(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_admin), skip: int = 0, limit: int = 100):
    users = db.query(User).offset(skip).limit(limit).all()
    return users
