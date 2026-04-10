from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from api.deps import get_current_user, get_current_user_profile, get_current_active_admin
from core.database import get_db_sync as get_db
from models.user import User
from schemas.user import UserResponse, UserUpdate
from services.match_stats import compute_match_stats_for_user
from services.restrictions import get_active_restrictions_for_user, serialize_user_restriction

router = APIRouter()


def _normalize_phone(phone_number: str) -> str:
    normalized = phone_number.strip().replace(" ", "")
    if len(normalized) == 10 and normalized.isdigit():
        normalized = f"+91{normalized}"
    return normalized


@router.get("/me", response_model=UserResponse)
def read_user_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    active_restrictions = get_active_restrictions_for_user(db, current_user.id)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "wallet_balance": float(current_user.wallet_balance or 0),
        "profile_pic": current_user.profile_pic,
        "bio": current_user.bio,
        "freefire_id": current_user.freefire_id,
        "is_active": bool(current_user.is_active),
        "face_image_path": getattr(current_user, "face_image_path", None),
        "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
    }


@router.get("/me/stats")
def read_user_me_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
):
    return compute_match_stats_for_user(db, current_user.id)


@router.put("/me", response_model=UserResponse)
def update_user_me(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile)
):
    if user_update.username is not None:
        current_user.username = user_update.username

    if user_update.bio is not None:
        cleaned_bio = user_update.bio.strip()
        current_user.bio = cleaned_bio or None
    if user_update.freefire_id is not None:
        current_user.freefire_id = user_update.freefire_id

    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    active_restrictions = get_active_restrictions_for_user(db, current_user.id)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "wallet_balance": float(current_user.wallet_balance or 0),
        "profile_pic": current_user.profile_pic,
        "bio": current_user.bio,
        "freefire_id": current_user.freefire_id,
        "is_active": bool(current_user.is_active),
        "face_image_path": getattr(current_user, "face_image_path", None),
        "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
    }


@router.get("/", response_model=List[UserResponse])
def read_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
    skip: int = 0,
    limit: int = 100
):
    users = db.query(User).offset(skip).limit(limit).all()
    return users
