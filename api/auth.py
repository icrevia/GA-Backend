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
    phone = user_in.phone_number.strip().replace(" ", "")
    # Auto-format 10 digit numbers to +91 (India)
    if len(phone) == 10 and phone.isdigit():
        phone = f"+91{phone}"

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    if db.query(User).filter(User.phone_number == phone).first():
        raise HTTPException(status_code=400, detail="Phone number already in use")

    if db.query(User).filter(User.username == user_in.username).first():
        raise HTTPException(status_code=400, detail="Username taken")

    # 1. Generate unique referral code for the new user
    import string, random
    def generate_code():
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    
    ref_code = generate_code()
    while db.query(User).filter(User.referral_code == ref_code).first():
        ref_code = generate_code()

    # 2. Check for referrer
    referrer = None
    if user_in.referral_code:
        referrer = db.query(User).filter(User.referral_code == user_in.referral_code.strip().upper()).first()
        if not referrer:
             logger.warning(f"Invalid referral code used: {user_in.referral_code}")
             # We don't block signup, just log it. Or could raise 400.

    db_user = User(
        username=user_in.username,
        email=email,
        phone_number=phone,
        hashed_password=hash_password(user_in.password),
        role="USER",
        referral_code=ref_code,
        referred_by_id=referrer.id if referrer else None,
        # Starting bonus for being referred ($20)
        wallet_balance=20.00 if referrer else 0.00
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # 3. Handle Referrer Payout ($50)
    if referrer:
        referrer.wallet_balance += 50.00
        
        from models.wallet import WalletTransaction
        # Record Referrer Transaction
        db.add(WalletTransaction(
            user_id=referrer.id,
            amount=50.00,
            transaction_type="REFERRAL_REWARD",
            status="SUCCESS",
            reference_id=f"REF_EARN_{db_user.id}"
        ))
        
        # Record New User Transaction
        db.add(WalletTransaction(
            user_id=db_user.id,
            amount=20.00,
            transaction_type="WELCOME_BONUS",
            status="SUCCESS",
            reference_id=f"WELCOME_{db_user.id}"
        ))
        
        from services.notifications import add_user_notification
        add_user_notification(
            db,
            referrer.id,
            "Referral Reward! 💎",
            f"Your friend {db_user.username} just joined. ₹50 added to your wallet!",
            "WALLET"
        )
        db.commit()

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

    token_version = getattr(db_user, "token_version", 0) or 0
    return {
        "access_token": create_access_token({"sub": str(db_user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": db_user
    }


@router.post("/login", response_model=SignupResponse)
@limiter.limit("10/minute")  # Rate limit: 10 login attempts per minute per IP
def login(request: Request, login_data: LoginRequest, db: Session = Depends(get_db)) -> Any:
    # Normalize identifier (could be email or phone)
    identifier = login_data.email.strip().lower()
    if identifier.isdigit() and len(identifier) == 10:
        identifier = f"+91{identifier}"

    user = db.query(User).filter(
        or_(
            User.email == identifier,
            User.username == login_data.email.strip(),
            User.phone_number == identifier
        )
    ).first()

    if not user:
        logger.warning(f"Login attempt for unknown identifier: {login_data.email[:30]}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found. Please sign up or check your identifier.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(login_data.password, user.hashed_password):
        logger.warning(f"Failed login attempt for user_id={user.id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password. Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Please contact support.",
        )

    logger.info(f"Successful login: user_id={user.id}")

    token_version = getattr(user, "token_version", 0) or 0
    return {
        "access_token": create_access_token({"sub": str(user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }
