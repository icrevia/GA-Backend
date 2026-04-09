from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select, func
from slowapi import Limiter
from slowapi.util import get_remote_address
from core.database import get_db
from core.config import settings
from core.security import hash_password, verify_password, create_access_token
from models.user import User
from schemas.user import UserCreate, UserResponse, LoginRequest
from schemas.token import Token
from typing import Any
from decimal import Decimal
import hashlib
import logging
import string
import random

# In-memory store
_otp_store: dict[str, str] = {}
_pending_signups: dict[str, dict] = {}

logger = logging.getLogger("GamerzAdda.auth")
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

def _normalize_signup_phone(raw_phone: str) -> str:
    phone = (raw_phone or "").strip().replace(" ", "")
    if len(phone) == 10 and phone.isdigit():
        phone = f"+91{phone}"
    return phone


def _is_admin_web_login_request(request: Request) -> bool:
    return (request.headers.get("x-login-source") or "").strip().lower() == "admin-web"


def _matches_admin_login_identifier(input_identifier: str) -> bool:
    configured_identifier = (settings.ADMIN_LOGIN_IDENTIFIER or "").strip().lower()
    configured_phone = _normalize_signup_phone(settings.ADMIN_LOGIN_PHONE)

    incoming_raw = (input_identifier or "").strip()
    incoming_identifier = incoming_raw.lower()
    incoming_phone = _normalize_signup_phone(incoming_raw)

    return bool(
        (configured_identifier and incoming_identifier == configured_identifier)
        or (configured_phone and incoming_phone == configured_phone)
    )

@router.get("/signup-availability")
@limiter.limit("60/minute")
async def signup_availability(
    request: Request,
    username: str | None = Query(default=None),
    email: str | None = Query(default=None),
    phone: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    normalized_username = (username or "").strip()
    normalized_email = (email or "").strip().lower().split("\n")[0]
    normalized_phone = _normalize_signup_phone(phone or "")

    username_available = True
    email_available = True
    phone_available = True

    if normalized_username:
        result = await db.execute(select(User.id).where(User.username == normalized_username))
        username_available = result.scalar_one_or_none() is None
    if normalized_email:
        result = await db.execute(select(User.id).where(User.email == normalized_email))
        email_available = result.scalar_one_or_none() is None
    if normalized_phone:
        result = await db.execute(select(User.id).where(User.phone_number == normalized_phone))
        phone_available = result.scalar_one_or_none() is None

    return {
        "username_available": username_available,
        "email_available": email_available,
        "phone_available": phone_available,
    }

@router.post("/signup")
@limiter.limit("5/minute")
async def signup(request: Request, user_in: UserCreate, db: AsyncSession = Depends(get_db)) -> Any:
    email = user_in.email.strip().lower().split('\n')[0]
    phone = _normalize_signup_phone(user_in.phone_number)

    # Email check
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Phone check
    result = await db.execute(select(User).where(User.phone_number == phone))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Phone number already in use")

    # Username check
    result = await db.execute(select(User).where(User.username == user_in.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username taken")

    _pending_signups[phone] = {
        "username": user_in.username,
        "email": email,
        "phone_number": phone,
        "referral_code": user_in.referral_code,
    }

    try:
        from services import otp as otp_service
        # Async call with await
        result = await otp_service.send_otp(phone)
        verification_id = result["data"]["verificationId"]
        _otp_store[phone] = verification_id
        return {"message": "OTP sent to phone for verification", "phone": phone, "status": "pending_verification"}
    except Exception as e:
        _pending_signups.pop(phone, None)
        logger.error(f"OTP send error during signup: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to send OTP verification. Error: {str(e)}")

@router.post("/verify-otp")
async def verify_otp(
    request: Request,
    phone: str = Query(...),
    otp: str = Query(...),
    db: AsyncSession = Depends(get_db)
) -> Any:
    normalized_phone = _normalize_signup_phone(phone)
    verification_id = _otp_store.get(normalized_phone)
    if not verification_id:
        raise HTTPException(status_code=400, detail="OTP expired or not requested.")

    from services import otp as otp_service
    # Async verify with await
    is_valid = await otp_service.verify_otp(verification_id, otp)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    _otp_store.pop(normalized_phone, None)
    
    # Existing user?
    result = await db.execute(select(User).where(User.phone_number == normalized_phone))
    db_user = result.scalar_one_or_none()

    if normalized_phone in _pending_signups:
        pending_data = _pending_signups.pop(normalized_phone)
        
        def generate_code():
            return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        ref_code = generate_code()
        # Ensure unique ref code
        while (await db.execute(select(User).where(User.referral_code == ref_code))).scalar_one_or_none():
            ref_code = generate_code()

        referrer = None
        if pending_data["referral_code"]:
            res = await db.execute(select(User).where(User.referral_code == pending_data["referral_code"].strip().upper()))
            referrer = res.scalar_one_or_none()

        db_user = User(
            username=pending_data["username"],
            email=pending_data["email"],
            phone_number=pending_data["phone_number"],
            role="USER",
            referral_code=ref_code,
            referred_by_id=referrer.id if referrer else None,
            wallet_balance=Decimal("0.00")
        )

        db.add(db_user)
        await db.commit()
        await db.refresh(db_user)

        # Referrer bonus?
        if referrer:
            referrer.wallet_balance = (referrer.wallet_balance or 0) + Decimal("2.00")
            from models.wallet import WalletTransaction
            db.add(WalletTransaction(
                user_id=referrer.id,
                amount=Decimal("2.00"),
                transaction_type="REFERRAL_REWARD",
                status="SUCCESS",
                reference_id=f"REF_SIGNUP_{db_user.id}"
            ))
            await db.commit()

        # Assign avatar
        db_user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{(db_user.id % 5) + 1}.png"
        await db.commit()
        await db.refresh(db_user)

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    token_version = getattr(db_user, "token_version", 0) or 0
    user_payload = UserResponse.model_validate(db_user).model_dump(mode="json")
    return {
        "access_token": create_access_token({"sub": str(db_user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": user_payload
    }

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, login_data: LoginRequest, db: AsyncSession = Depends(get_db)) -> Any:
    raw_identifier = login_data.email.strip()
    identifier = raw_identifier.lower()
    if identifier.isdigit() and len(identifier) == 10:
        identifier = f"+91{identifier}"

    user: User | None = None

    if _is_admin_web_login_request(request):
        configured_identifier = (settings.ADMIN_LOGIN_IDENTIFIER or "").strip()
        configured_phone = _normalize_signup_phone(settings.ADMIN_LOGIN_PHONE)

        if not configured_identifier or not configured_phone:
            logger.error("Admin-web login blocked: ADMIN_LOGIN_IDENTIFIER/ADMIN_LOGIN_PHONE not configured")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Admin login is not configured. "
                    "Set ADMIN_LOGIN_IDENTIFIER and ADMIN_LOGIN_PHONE in Railway variables."
                ),
            )

        if not _matches_admin_login_identifier(raw_identifier):
            raise HTTPException(status_code=403, detail="Invalid admin credentials")

        result = await db.execute(
            select(User).where(
                User.phone_number == configured_phone,
                User.role == "ADMIN",
            )
        )
        user = result.scalar_one_or_none()

        if not user:
            logger.error("Admin-web login blocked: no ADMIN user matched ADMIN_LOGIN_PHONE")
            raise HTTPException(
                status_code=403,
                detail="Admin account is not provisioned for configured Railway credentials",
            )

        if not user.is_active:
            raise HTTPException(status_code=403, detail="Admin account is disabled")
    else:
        result = await db.execute(
            select(User).where(
                or_(
                    User.email == identifier,
                    User.username == raw_identifier,
                    User.phone_number == identifier
                )
            )
        )
        user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        from services import otp as otp_service
        # Async call with await
        res = await otp_service.send_otp(user.phone_number)
        _otp_store[user.phone_number] = res["data"]["verificationId"]
        logger.info(f"OTP successfully sent for login: {user.phone_number}")
        return {"message": "OTP sent", "phone": user.phone_number, "status": "pending_verification"}
    except Exception as e:
        logger.error(f"OTP SEND ERR LOGIN: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to send OTP login: {str(e)}")


@router.post("/send-otp")
@limiter.limit("10/minute")
async def send_otp(
    request: Request,
    phone: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Resend OTP for login/signup continuation flows.

    Supports existing users (login) and pending signups already present in memory.
    """
    normalized_phone = _normalize_signup_phone(phone)

    user_exists_res = await db.execute(select(User.id).where(User.phone_number == normalized_phone))
    user_exists = user_exists_res.scalar_one_or_none() is not None

    if not user_exists and normalized_phone not in _pending_signups:
        raise HTTPException(status_code=404, detail="Account not found for this phone")

    try:
        from services import otp as otp_service
        res = await otp_service.send_otp(normalized_phone)
        _otp_store[normalized_phone] = res["data"]["verificationId"]
        return {"message": "OTP sent", "phone": normalized_phone, "status": "pending_verification"}
    except Exception as e:
        logger.error(f"OTP SEND ERR RESEND: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to resend OTP: {str(e)}")
