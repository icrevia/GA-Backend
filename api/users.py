from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from api.deps import get_current_user, get_current_active_admin
from core.database import get_db
from models.user import User
from schemas.user import UserResponse, UserUpdate

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


@router.get("/", response_model=List[UserResponse])
def read_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
    skip: int = 0,
    limit: int = 100
):
    users = db.query(User).offset(skip).limit(limit).all()
    return users
