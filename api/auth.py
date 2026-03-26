from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from slowapi import Limiter
from slowapi.util import get_remote_address
from core.database import get_db
from core.config import settings
from core.security import hash_password, verify_password, create_access_token
from models.user import User
from schemas.user import UserCreate, UserResponse, LoginRequest
from schemas.token import Token
from typing import Any
import logging

logger = logging.getLogger("zexplay.auth")
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


class SignupResponse(Token):
    user: UserResponse


@router.post("/signup", response_model=SignupResponse)
@limiter.limit("5/minute")  # Rate limit: 5 signups per minute per IP
def signup(request: Request, user_in: UserCreate, db: Session = Depends(get_db)) -> Any:
    email = user_in.email.strip().lower().split('\n')[0]

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    if db.query(User).filter(User.username == user_in.username).first():
        raise HTTPException(status_code=400, detail="Username taken")

    db_user = User(
        username=user_in.username,
        email=email,
        hashed_password=hash_password(user_in.password),
        role="USER",
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # Auto-assign one of 5 default avatars
    avatar_id = (db_user.id % 5) + 1
    db_user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{avatar_id}.png"
    db.commit()
    db.refresh(db_user)

    from services.notifications import add_user_notification
    add_user_notification(
        db,
        db_user.id,
        "Welcome to ZexPlay",
        "Start your esports journey with India's fastest tournament platform. 🦾",
        "APP"
    )

    logger.info(f"New signup: user_id={db_user.id} username={db_user.username}")

    return {
        "access_token": create_access_token({"sub": str(db_user.id)}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": db_user
    }


@router.post("/login", response_model=SignupResponse)
@limiter.limit("10/minute")  # Rate limit: 10 login attempts per minute per IP
def login(request: Request, login_data: LoginRequest, db: Session = Depends(get_db)) -> Any:
    user = db.query(User).filter(
        or_(
            User.email == login_data.email.strip().lower(),
            User.username == login_data.email.strip()
        )
    ).first()

    # FIXED: Generic error message — does not reveal whether email exists or not
    GENERIC_AUTH_ERROR = "Invalid credentials"

    if not user:
        logger.warning(f"Login attempt for unknown identifier: {login_data.email[:30]}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(login_data.password, user.hashed_password):
        logger.warning(f"Failed login attempt for user_id={user.id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Please contact support.",
        )

    logger.info(f"Successful login: user_id={user.id}")

    return {
        "access_token": create_access_token({"sub": str(user.id)}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }
