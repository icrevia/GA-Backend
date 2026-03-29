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
import hashlib
import logging
from services.login_security import (
    extract_client_ip,
    is_ip_blocked,
    record_failed_login,
    clear_failed_logins,
)
from services.telegram_alerts import send_security_alert_async

logger = logging.getLogger("zexplay.auth")
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()


def _mask_identifier(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "-"

    if "@" in raw:
        local, domain = raw.split("@", 1)
        if not local:
            return f"***@{domain}"
        if len(local) == 1:
            local_masked = "*"
        elif len(local) == 2:
            local_masked = f"{local[0]}*"
        else:
            local_masked = f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"
        return f"{local_masked}@{domain}"

    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return f"***{digits[-4:]}"

    if len(raw) <= 3:
        return "***"
    return f"{raw[0]}***{raw[-1]}"


def _safe_user_agent(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "-").replace("\n", " ").strip()
    return ua[:180]


def _safe_header(request: Request, header_name: str, max_len: int = 180) -> str:
    value = (request.headers.get(header_name) or "").replace("\n", " ").strip()
    if not value:
        return "-"
    return value[:max_len]


def _build_request_context(request: Request, client_ip: str) -> dict[str, object]:
    user_agent = _safe_user_agent(request)
    accept_language = _safe_header(request, "accept-language", 120)
    platform = _safe_header(request, "sec-ch-ua-platform", 80)
    browser_hint = _safe_header(request, "sec-ch-ua", 120)
    origin = _safe_header(request, "origin", 140)
    referer = _safe_header(request, "referer", 180)
    forwarded_for = _safe_header(request, "x-forwarded-for", 140)
    real_ip = _safe_header(request, "x-real-ip", 80)
    cf_connecting_ip = _safe_header(request, "cf-connecting-ip", 80)

    fingerprint_source = "|".join([client_ip, user_agent, accept_language, platform, browser_hint])
    device_fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12]

    context: dict[str, object] = {
        "ip": client_ip,
        "user_agent": user_agent,
        "device_fingerprint": device_fingerprint,
    }

    optional_headers = {
        "accept_language": accept_language,
        "platform": platform,
        "browser_hint": browser_hint,
        "origin": origin,
        "referer": referer,
        "forwarded_for": forwarded_for,
        "real_ip": real_ip,
        "cf_connecting_ip": cf_connecting_ip,
    }

    for key, value in optional_headers.items():
        if value != "-":
            context[key] = value

    return context


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
    client_ip = extract_client_ip(request)
    request_context = _build_request_context(request, client_ip)
    raw_identifier = login_data.email.strip()

    # Normalize identifier (could be email or phone)
    identifier = raw_identifier.lower()
    if identifier.isdigit() and len(identifier) == 10:
        identifier = f"+91{identifier}"

    blocked, retry_after_seconds = is_ip_blocked(client_ip)
    if blocked:
        logger.warning("Blocked login attempt from ip=%s", client_ip)
        send_security_alert_async(
            event="LOGIN_BLOCKED_IP_HIT",
            details={
                **request_context,
                "retry_after_seconds": retry_after_seconds,
                "identifier": _mask_identifier(raw_identifier),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts from this IP. Try again in {retry_after_seconds} seconds.",
        )

    user = db.query(User).filter(
        or_(
            User.email == identifier,
            User.username == raw_identifier,
            User.phone_number == identifier
        )
    ).first()

    if not user:
        attempts, blocked_now, blocked_for_seconds = record_failed_login(client_ip)
        logger.warning("Login attempt for unknown identifier from ip=%s", client_ip)
        send_security_alert_async(
            event="LOGIN_FAILED_UNKNOWN_IDENTIFIER",
            details={
                **request_context,
                "attempts_in_window": attempts,
                "identifier": _mask_identifier(raw_identifier),
                "blocked_now": blocked_now,
                "block_seconds": blocked_for_seconds,
            },
        )

        if blocked_now:
            send_security_alert_async(
                event="LOGIN_IP_BLOCKED",
                details={
                    **request_context,
                    "reason": "too_many_failed_logins",
                    "block_seconds": blocked_for_seconds,
                },
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(login_data.password, user.hashed_password):
        attempts, blocked_now, blocked_for_seconds = record_failed_login(client_ip)
        logger.warning("Failed login attempt for user_id=%s from ip=%s", user.id, client_ip)
        send_security_alert_async(
            event="LOGIN_FAILED_BAD_PASSWORD",
            details={
                **request_context,
                "attempts_in_window": attempts,
                "blocked_now": blocked_now,
                "block_seconds": blocked_for_seconds,
                "user_id": user.id,
                "username": user.username,
                "identifier": _mask_identifier(raw_identifier),
            },
        )

        if blocked_now:
            send_security_alert_async(
                event="LOGIN_IP_BLOCKED",
                details={
                    **request_context,
                    "reason": "too_many_failed_logins",
                    "block_seconds": blocked_for_seconds,
                    "last_user_id": user.id,
                },
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clear_failed_logins(client_ip)
    logger.info(f"Successful login: user_id={user.id}")
    send_security_alert_async(
        event="LOGIN_SUCCESS",
        details={
            **request_context,
            "user_id": user.id,
            "username": user.username,
            "email": _mask_identifier(user.email),
            "role": user.role,
        },
    )

    token_version = getattr(user, "token_version", 0) or 0
    return {
        "access_token": create_access_token({"sub": str(user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": user.role,
        "user": user
    }


