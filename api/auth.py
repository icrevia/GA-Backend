from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select, func
from slowapi import Limiter
from slowapi.util import get_remote_address
from core.database import get_db
from core.config import settings
from core.security import hash_password, verify_password, create_access_token
from core.websockets import manager
from models.user import User
from schemas.user import UserCreate, UserResponse, LoginRequest
from schemas.token import Token
from typing import Any
from decimal import Decimal
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import string
import random
import httpx
from services.login_security import extract_client_ip

# In-memory store
_otp_store: dict[str, str] = {}
_pending_signups: dict[str, dict] = {}
_admin_login_otp_store: dict[str, dict[str, Any]] = {}

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


def _phone_variants(raw_phone: str) -> set[str]:
    raw = (raw_phone or "").strip().replace(" ", "")
    if not raw:
        return set()

    variants: set[str] = {raw}
    normalized = _normalize_signup_phone(raw)
    variants.add(normalized)

    digits = "".join(ch for ch in normalized if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        variants.add(digits[2:])
        variants.add(f"+{digits}")
    elif len(digits) == 10:
        variants.add(digits)
        variants.add(f"+91{digits}")

    return {value for value in variants if value}


def _admin_seed_from_identifier(configured_identifier: str, configured_phone: str) -> str:
    raw_identifier = (configured_identifier or "").strip().lower()
    if "@" in raw_identifier:
        return raw_identifier.split("@", 1)[0]

    digits = "".join(ch for ch in configured_phone if ch.isdigit())
    if len(digits) >= 4:
        return f"admin_{digits[-4:]}"
    return "admin_env"


def _sanitize_username(raw: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in (raw or ""))
    cleaned = cleaned.strip("._-")
    if len(cleaned) < 3:
        cleaned = "admin_env"
    return cleaned[:32]


def _sanitize_email_local_part(raw: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "._+-") else "_" for ch in (raw or ""))
    cleaned = cleaned.strip("._+-")
    if not cleaned:
        cleaned = "admin_env"
    return cleaned[:48]


def _resolve_login_device(request: Request) -> str:
    preferred = (request.headers.get("x-device-name") or "").strip()
    if preferred:
        return preferred[:160]

    user_agent = (request.headers.get("user-agent") or "").strip()
    if user_agent:
        return user_agent[:160]

    return "Unknown Device"


def _build_banned_support_response(user: User, fallback_phone: str) -> dict[str, str]:
    token_version = getattr(user, "token_version", 0) or 0
    support_token = create_access_token({"sub": str(user.id), "tv": token_version})
    return {
        "message": "Your account is restricted. Redirecting you to Live Chat support.",
        "status": "banned_support",
        "phone": (user.phone_number or fallback_phone or "").strip(),
        "access_token": support_token,
        "role": (user.role or "USER").strip() or "USER",
    }


async def _is_blocked_for_login_support(db: AsyncSession, user: User) -> bool:
    return not bool(user.is_active)


def _clean_env_value(value: str | None) -> str:
    return str(value or "").strip().strip('"\'')


def _admin_login_phone_key() -> str:
    return _normalize_signup_phone(settings.ADMIN_LOGIN_PHONE)


def _is_admin_login_phone_value(raw_phone: str) -> bool:
    configured_variants = _phone_variants(settings.ADMIN_LOGIN_PHONE)
    incoming_variants = _phone_variants(raw_phone)
    return bool(configured_variants and incoming_variants and not configured_variants.isdisjoint(incoming_variants))


def _resolve_admin_login_chat_id() -> str:
    return _clean_env_value(settings.ADMIN_LOGIN_TELEGRAM_CHAT_ID or settings.TELEGRAM_ALERT_CHAT_ID)


def _generate_admin_login_otp(length: int = 4) -> str:
    return "".join(random.choices(string.digits, k=length))


async def _send_admin_login_otp_to_telegram(*, otp_code: str, phone: str, identifier: str) -> None:
    bot_token = _clean_env_value(settings.TELEGRAM_BOT_TOKEN)
    chat_id = _resolve_admin_login_chat_id()

    if not bot_token:
        raise RuntimeError("Admin OTP bot token is missing. Set TELEGRAM_BOT_TOKEN in Railway.")
    if not chat_id:
        raise RuntimeError(
            "Admin Telegram chat ID is missing. Set ADMIN_LOGIN_TELEGRAM_CHAT_ID in Railway."
        )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": (
            "GamerzAdda Admin Login OTP\n"
            f"OTP: {otp_code}\n"
            "Valid for 5 minutes.\n"
            f"Identifier: {identifier or '--'}\n"
            f"Phone: {phone}\n"
            "Do not share this code."
        ),
        "disable_web_page_preview": True,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code >= 400:
        logger.error(
            "Admin OTP Telegram send failed. status=%s body=%s",
            response.status_code,
            (response.text or "")[:240],
        )
        raise RuntimeError("Failed to send OTP on Telegram")

    try:
        body = response.json()
    except Exception:
        body = {}

    if isinstance(body, dict) and body.get("ok") is False:
        logger.error("Admin OTP Telegram rejected by API: %s", body)
        raise RuntimeError("Failed to send OTP on Telegram")


async def _issue_admin_login_otp(*, identifier: str, phone: str) -> None:
    otp_code = _generate_admin_login_otp(4)
    phone_key = _admin_login_phone_key() or _normalize_signup_phone(phone)
    _admin_login_otp_store[phone_key] = {
        "otp_hash": hashlib.sha256(otp_code.encode("utf-8")).hexdigest(),
        "expires_at": datetime.utcnow() + timedelta(minutes=5),
        "attempts": 0,
    }

    try:
        await _send_admin_login_otp_to_telegram(
            otp_code=otp_code,
            phone=phone_key,
            identifier=(identifier or "").strip(),
        )
    except Exception:
        _admin_login_otp_store.pop(phone_key, None)
        raise


def _verify_admin_login_otp(*, phone: str, otp_code: str) -> tuple[bool, str]:
    phone_key = _admin_login_phone_key() or _normalize_signup_phone(phone)
    otp_entry = _admin_login_otp_store.get(phone_key)
    if not otp_entry:
        return False, "missing"

    if datetime.utcnow() > otp_entry.get("expires_at", datetime.utcnow()):
        _admin_login_otp_store.pop(phone_key, None)
        return False, "expired"

    attempts = int(otp_entry.get("attempts", 0) or 0)
    if attempts >= 5:
        _admin_login_otp_store.pop(phone_key, None)
        return False, "expired"

    incoming_hash = hashlib.sha256((otp_code or "").strip().encode("utf-8")).hexdigest()
    stored_hash = str(otp_entry.get("otp_hash") or "")
    if not hmac.compare_digest(incoming_hash, stored_hash):
        otp_entry["attempts"] = attempts + 1
        if otp_entry["attempts"] >= 5:
            _admin_login_otp_store.pop(phone_key, None)
            return False, "expired"
        _admin_login_otp_store[phone_key] = otp_entry
        return False, "invalid"

    _admin_login_otp_store.pop(phone_key, None)
    return True, "ok"


async def _build_unique_username(db: AsyncSession, base_username: str) -> str:
    base = _sanitize_username(base_username)
    for idx in range(0, 50):
        suffix = "" if idx == 0 else f"_{idx}"
        candidate = f"{base[: max(1, 32 - len(suffix))]}{suffix}"
        result = await db.execute(select(User.id).where(User.username == candidate))
        if result.scalar_one_or_none() is None:
            return candidate
    return f"admin_{random.randint(1000, 9999)}"


async def _build_unique_email(db: AsyncSession, preferred_email: str) -> str:
    normalized = (preferred_email or "").strip().lower()
    if "@" in normalized:
        local_part, domain_part = normalized.split("@", 1)
    else:
        local_part, domain_part = normalized, "gamerzadda.local"

    local_part = _sanitize_email_local_part(local_part)
    domain_part = (domain_part or "gamerzadda.local").strip().strip(".") or "gamerzadda.local"

    for idx in range(0, 50):
        suffix = "" if idx == 0 else f"+{idx}"
        local_candidate = f"{local_part[: max(1, 64 - len(suffix))]}{suffix}"
        candidate = f"{local_candidate}@{domain_part}"
        result = await db.execute(select(User.id).where(func.lower(User.email) == candidate.lower()))
        if result.scalar_one_or_none() is None:
            return candidate

    return f"admin_{random.randint(1000, 9999)}@gamerzadda.local"


async def _provision_or_activate_env_admin_user(
    db: AsyncSession,
    configured_identifier: str,
    configured_phone: str,
) -> User | None:
    configured_identifier_lower = (configured_identifier or "").strip().lower()
    phone_variants = list(_phone_variants(configured_phone))

    lookup_conditions = [
        func.lower(User.email) == configured_identifier_lower,
        func.lower(User.username) == configured_identifier_lower,
    ]
    if phone_variants:
        lookup_conditions.append(User.phone_number.in_(phone_variants))

    existing_result = await db.execute(
        select(User)
        .where(or_(*lookup_conditions))
        .order_by(User.id.asc())
    )
    existing_user = existing_result.scalars().first()

    if existing_user:
        changed = False
        if existing_user.role != "ADMIN":
            existing_user.role = "ADMIN"
            changed = True
        if not existing_user.is_active:
            existing_user.is_active = True
            changed = True
        if configured_phone and not existing_user.phone_number:
            existing_user.phone_number = configured_phone
            changed = True

        if changed:
            await db.commit()
            await db.refresh(existing_user)
            logger.info("Admin-web login upgraded existing user id=%s to active ADMIN", existing_user.id)

        return existing_user

    username_seed = _admin_seed_from_identifier(configured_identifier, configured_phone)
    preferred_email = (
        configured_identifier_lower
        if "@" in configured_identifier_lower
        else f"{_sanitize_email_local_part(username_seed)}@gamerzadda.local"
    )

    username = await _build_unique_username(db, username_seed)
    email = await _build_unique_email(db, preferred_email)

    new_admin = User(
        username=username,
        email=email,
        phone_number=configured_phone,
        role="ADMIN",
        is_active=True,
        wallet_balance=Decimal("0.00"),
    )
    db.add(new_admin)

    try:
        await db.commit()
        await db.refresh(new_admin)
        logger.info("Admin-web login auto-provisioned ADMIN user id=%s", new_admin.id)
        return new_admin
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to auto-provision ADMIN user from env credentials: %s", exc)
        return None

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
    phone_restricted = False
    status: str | None = None
    message: str | None = None
    access_token: str | None = None
    role: str | None = None

    if normalized_username:
        result = await db.execute(select(User.id).where(User.username == normalized_username))
        username_available = result.scalar_one_or_none() is None
    if normalized_email:
        result = await db.execute(select(User.id).where(User.email == normalized_email))
        email_available = result.scalar_one_or_none() is None
    if normalized_phone:
        phone_candidates = list(_phone_variants(normalized_phone))
        if phone_candidates:
            result = await db.execute(
                select(User).where(User.phone_number.in_(phone_candidates)).order_by(User.id.asc())
            )
        else:
            result = await db.execute(
                select(User).where(User.phone_number == normalized_phone).order_by(User.id.asc())
            )

        matched_user = result.scalars().first()
        phone_available = matched_user is None

        if matched_user and await _is_blocked_for_login_support(db, matched_user):
            phone_restricted = True
            payload = _build_banned_support_response(matched_user, normalized_phone)
            status = payload.get("status")
            message = payload.get("message")
            access_token = payload.get("access_token")
            role = payload.get("role")

    return {
        "username_available": username_available,
        "email_available": email_available,
        "phone_available": phone_available,
        "phone_restricted": phone_restricted,
        "status": status,
        "message": message,
        "access_token": access_token,
        "role": role,
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
    is_admin_phone = _is_admin_login_phone_value(normalized_phone)

    if is_admin_phone:
        is_valid, reason = _verify_admin_login_otp(phone=normalized_phone, otp_code=otp)
        if not is_valid:
            if reason in {"missing", "expired"}:
                raise HTTPException(status_code=400, detail="OTP expired or not requested. Please resend.")
            raise HTTPException(status_code=400, detail="Invalid OTP")
    else:
        verification_id = _otp_store.get(normalized_phone)
        if not verification_id:
            raise HTTPException(status_code=400, detail="OTP expired or not requested.")

        from services import otp as otp_service
        # Async verify with await
        try:
            is_valid = await otp_service.verify_otp(verification_id, otp)
        except Exception as e:
            logger.error(f"OTP verify provider error: {e}")
            raise HTTPException(
                status_code=503,
                detail="OTP verification service is temporarily unavailable. Please retry in 30 seconds."
            )

        if not is_valid:
            raise HTTPException(status_code=400, detail="Invalid OTP")

        _otp_store.pop(normalized_phone, None)
    
    # Existing user?
    phone_candidates = list(_phone_variants(normalized_phone))
    if phone_candidates:
        result = await db.execute(select(User).where(User.phone_number.in_(phone_candidates)))
    else:
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

        # Assign avatar
        db_user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{(db_user.id % 5) + 1}.png"
        await db.commit()
        await db.refresh(db_user)

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    client_ip = extract_client_ip(request)
    device_name = _resolve_login_device(request)

    await manager.force_logout_user(
        db_user.id,
        reason=f"Account logged in from {device_name} (IP {client_ip})."
    )

    db_user.token_version = (getattr(db_user, "token_version", 0) or 0) + 1
    db_user.last_login_ip = (client_ip or "")[:64] or None
    db_user.last_login_device = device_name
    db_user.last_login_at = datetime.utcnow()
    await db.commit()
    await db.refresh(db_user)

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

        configured_identifier_lower = configured_identifier.lower()
        phone_variants = list(_phone_variants(settings.ADMIN_LOGIN_PHONE))

        admin_match_conditions = [
            func.lower(User.email) == configured_identifier_lower,
            func.lower(User.username) == configured_identifier_lower,
        ]
        if phone_variants:
            admin_match_conditions.append(User.phone_number.in_(phone_variants))

        result = await db.execute(
            select(User)
            .where(
                User.role == "ADMIN",
                or_(*admin_match_conditions),
            )
            .order_by(User.id.asc())
        )
        user = result.scalars().first()

        if user and not user.is_active:
            user.is_active = True
            await db.commit()
            await db.refresh(user)

        if not user:
            user = await _provision_or_activate_env_admin_user(
                db,
                configured_identifier=configured_identifier,
                configured_phone=configured_phone,
            )

        if not user:
            logger.error("Admin-web login blocked: unable to provision active ADMIN user")
            raise HTTPException(
                status_code=403,
                detail="Admin account provisioning failed",
            )
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

    if not _is_admin_web_login_request(request):
        if await _is_blocked_for_login_support(db, user):
            return _build_banned_support_response(user, identifier)

    if _is_admin_web_login_request(request):
        admin_phone = _admin_login_phone_key()
        if not admin_phone:
            raise HTTPException(status_code=503, detail="Admin login phone is not configured")

        try:
            await _issue_admin_login_otp(
                identifier=raw_identifier,
                phone=admin_phone,
            )
            _otp_store.pop(admin_phone, None)
            logger.info("Admin login OTP sent on Telegram for %s", admin_phone)
            return {
                "message": "OTP sent on Telegram",
                "phone": admin_phone,
                "status": "pending_verification",
            }
        except Exception as e:
            logger.error("ADMIN OTP SEND ERR LOGIN: %s", e)
            raise HTTPException(status_code=503, detail=f"Failed to send admin OTP: {str(e)}")

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

    if _is_admin_login_phone_value(normalized_phone):
        admin_phone = _admin_login_phone_key() or normalized_phone
        admin_phone_variants = list(_phone_variants(admin_phone))
        if admin_phone_variants:
            admin_res = await db.execute(
                select(User.id).where(
                    User.role == "ADMIN",
                    User.phone_number.in_(admin_phone_variants),
                )
            )
        else:
            admin_res = await db.execute(
                select(User.id).where(
                    User.role == "ADMIN",
                    User.phone_number == admin_phone,
                )
            )

        if admin_res.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Admin account not found for configured phone")

        try:
            await _issue_admin_login_otp(
                identifier=settings.ADMIN_LOGIN_IDENTIFIER or admin_phone,
                phone=admin_phone,
            )
            _otp_store.pop(admin_phone, None)
            return {
                "message": "OTP sent on Telegram",
                "phone": admin_phone,
                "status": "pending_verification",
            }
        except Exception as e:
            logger.error("ADMIN OTP SEND ERR RESEND: %s", e)
            raise HTTPException(status_code=503, detail=f"Failed to resend admin OTP: {str(e)}")

    phone_candidates = list(_phone_variants(normalized_phone))
    if phone_candidates:
        user_result = await db.execute(select(User).where(User.phone_number.in_(phone_candidates)))
    else:
        user_result = await db.execute(select(User).where(User.phone_number == normalized_phone))
    existing_user = user_result.scalar_one_or_none()
    if existing_user and await _is_blocked_for_login_support(db, existing_user):
        return _build_banned_support_response(existing_user, normalized_phone)

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
