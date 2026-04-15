from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select, func
from slowapi import Limiter
from slowapi.util import get_remote_address
from core.database import get_db, SessionLocal
from core.config import settings
from core.security import hash_password, verify_password, create_access_token
from core.websockets import manager
from models.user import User
from models.email_otp_log import EmailOtpLog
from schemas.user import UserCreate, UserResponse, LoginRequest
from schemas.token import Token
from typing import Any
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import hashlib
import hmac
import logging
import string
import random
import httpx
from services.login_security import extract_client_ip
from services.email_otp import is_email_otp_available, send_login_otp_email
from services.referral_codes import generate_unique_referral_code_async
from services.restrictions import RESTRICTION_SCOPE_FULL_APP, get_active_restrictions_for_user_async
from services.otp_limits import (
    OTP_LOCK_CLIENT_MESSAGE,
    OTP_LOCK_STATUS,
    get_active_phone_lock_async,
    register_otp_send_success_async,
    reset_otp_lock_after_success_async,
)
from services.activity_limits import (
    ensure_login_session_lock_not_blocking_async,
    register_login_session_success_async,
)

# In-memory store
_otp_store: dict[str, str] = {}
_pending_signups: dict[str, dict] = {}
_admin_login_otp_store: dict[str, dict[str, Any]] = {}
_email_login_otp_store: dict[str, dict[str, Any]] = {}

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
    if not bool(user.is_active):
        return True

    active_full_app_restrictions = await get_active_restrictions_for_user_async(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    return bool(active_full_app_restrictions)


async def _raise_if_phone_otp_locked(db: AsyncSession, phone: str) -> None:
    active_lock = await get_active_phone_lock_async(db, phone)
    if active_lock:
        raise HTTPException(status_code=429, detail=OTP_LOCK_CLIENT_MESSAGE)


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


def _generate_email_login_otp(length: int) -> str:
    safe_length = max(4, min(length, 8))
    return "".join(random.choices(string.digits, k=safe_length))


def _email_login_otp_digest(*, phone: str, email: str, otp_code: str) -> str:
    payload = f"{settings.SECRET_KEY}|email-login-otp|{_normalize_signup_phone(phone)}|{(email or '').strip().lower()}|{(otp_code or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mask_email(email: str) -> str:
    normalized = (email or "").strip().lower()
    if "@" not in normalized:
        return ""

    local, domain = normalized.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = local[:1] + ("*" * max(1, len(local) - 2)) + local[-1:]
    return f"{masked_local}@{domain}"


def _store_email_login_otp(*, phone: str, email: str, user_id: int | None, otp_code: str) -> None:
    normalized_phone = _normalize_signup_phone(phone)
    _email_login_otp_store[normalized_phone] = {
        "otp_hash": _email_login_otp_digest(phone=normalized_phone, email=email, otp_code=otp_code),
        "expires_at": datetime.utcnow() + timedelta(seconds=max(30, int(settings.EMAIL_OTP_TTL_SECONDS))),
        "attempts": 0,
        "max_attempts": max(1, int(settings.EMAIL_OTP_MAX_VERIFY_ATTEMPTS)),
        "email": (email or "").strip().lower(),
        "user_id": user_id,
    }


def _clear_email_login_otp(phone: str) -> None:
    _email_login_otp_store.pop(_normalize_signup_phone(phone), None)


def _verify_email_login_otp(*, phone: str, otp_code: str) -> tuple[bool, str, dict[str, Any] | None]:
    normalized_phone = _normalize_signup_phone(phone)
    otp_entry = _email_login_otp_store.get(normalized_phone)
    if not otp_entry:
        return False, "missing", None

    expires_at = otp_entry.get("expires_at")
    if not isinstance(expires_at, datetime) or datetime.utcnow() > expires_at:
        _email_login_otp_store.pop(normalized_phone, None)
        return False, "expired", otp_entry

    attempts = int(otp_entry.get("attempts", 0) or 0)
    max_attempts = int(otp_entry.get("max_attempts", settings.EMAIL_OTP_MAX_VERIFY_ATTEMPTS) or settings.EMAIL_OTP_MAX_VERIFY_ATTEMPTS)
    if attempts >= max_attempts:
        _email_login_otp_store.pop(normalized_phone, None)
        return False, "expired", otp_entry

    incoming_hash = _email_login_otp_digest(
        phone=normalized_phone,
        email=str(otp_entry.get("email") or ""),
        otp_code=otp_code,
    )
    stored_hash = str(otp_entry.get("otp_hash") or "")
    if not hmac.compare_digest(incoming_hash, stored_hash):
        otp_entry["attempts"] = attempts + 1
        if otp_entry["attempts"] >= max_attempts:
            _email_login_otp_store.pop(normalized_phone, None)
            return False, "expired", otp_entry
        _email_login_otp_store[normalized_phone] = otp_entry
        return False, "invalid", otp_entry

    _email_login_otp_store.pop(normalized_phone, None)
    return True, "ok", otp_entry


async def _log_email_otp_event(
    db: AsyncSession,
    *,
    user: User | None,
    email: str | None,
    phone: str | None,
    source: str,
    event_type: str,
    status: str,
    message: str | None,
    request: Request,
    commit: bool = False,
) -> None:
    try:
        user_agent = (request.headers.get("user-agent") or "").strip()[:220] or None
        client_ip = extract_client_ip(request)
        log_row = EmailOtpLog(
            user_id=user.id if user else None,
            email=(email or "").strip().lower() or None,
            phone_number=_normalize_signup_phone(phone or "") or None,
            source=(source or "LOGIN").strip().upper()[:32],
            event_type=(event_type or "SEND").strip().upper()[:16],
            status=(status or "UNKNOWN").strip().upper()[:24],
            message=(message or "").strip()[:400] or None,
            client_ip=(client_ip or "").strip()[:64] or None,
            user_agent=user_agent,
        )
        db.add(log_row)
        if commit:
            await db.commit()
    except Exception as exc:
        logger.warning("EMAIL OTP LOG WRITE ERR: %s", exc)
        if commit:
            try:
                await db.rollback()
            except Exception:
                pass


async def _send_login_email_otp_in_background(
    *,
    user_id: int | None,
    username: str | None,
    email: str,
    phone: str,
    source: str,
    otp_code: str,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    status = "SENT"
    message = "Email OTP sent"

    try:
        await send_login_otp_email(
            to_email=email,
            otp_code=otp_code,
            recipient_name=username,
        )
    except Exception as exc:
        _clear_email_login_otp(phone)
        status = "FAILED"
        message = f"Send failed: {str(exc)[:260]}"
        logger.warning("EMAIL OTP SEND ERR for %s: %s", phone, exc)

    try:
        async with SessionLocal() as bg_db:
            log_row = EmailOtpLog(
                user_id=user_id,
                email=(email or "").strip().lower() or None,
                phone_number=_normalize_signup_phone(phone or "") or None,
                source=(source or "LOGIN").strip().upper()[:32],
                event_type="SEND",
                status=status,
                message=(message or "").strip()[:400] or None,
                client_ip=(client_ip or "").strip()[:64] or None,
                user_agent=(user_agent or "").strip()[:220] or None,
            )
            bg_db.add(log_row)
            await bg_db.commit()
    except Exception as log_exc:
        logger.warning("EMAIL OTP LOG WRITE ERR: %s", log_exc)


async def _send_login_email_otp_if_available(
    *,
    request: Request,
    db: AsyncSession,
    user: User,
    source: str,
) -> tuple[bool, str | None, bool]:
    normalized_phone = _normalize_signup_phone(user.phone_number or "")
    normalized_email = (user.email or "").strip().lower()
    if not normalized_phone or not normalized_email or not is_email_otp_available():
        return False, None, False

    otp_code = _generate_email_login_otp(int(settings.EMAIL_OTP_LENGTH))
    _store_email_login_otp(
        phone=normalized_phone,
        email=normalized_email,
        user_id=user.id,
        otp_code=otp_code,
    )

    masked_email = _mask_email(normalized_email)
    client_ip = extract_client_ip(request)
    user_agent = (request.headers.get("user-agent") or "").strip()[:220] or None

    await _log_email_otp_event(
        db,
        user=user,
        email=normalized_email,
        phone=normalized_phone,
        source=source,
        event_type="SEND",
        status="QUEUED",
        message="Email OTP queued",
        request=request,
        commit=True,
    )

    try:
        asyncio.create_task(
            _send_login_email_otp_in_background(
                user_id=user.id,
                username=user.username,
                email=normalized_email,
                phone=normalized_phone,
                source=source,
                otp_code=otp_code,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        return False, masked_email, True
    except Exception as exc:
        _clear_email_login_otp(normalized_phone)
        await _log_email_otp_event(
            db,
            user=user,
            email=normalized_email,
            phone=normalized_phone,
            source=source,
            event_type="SEND",
            status="FAILED",
            message=f"Queue failed: {str(exc)[:260]}",
            request=request,
            commit=True,
        )
        logger.warning("EMAIL OTP QUEUE ERR for %s: %s", normalized_phone, exc)
        return False, None, False


def _build_login_otp_response(
    *,
    phone: str,
    email_sent: bool,
    masked_email: str | None,
    email_queued: bool = False,
) -> dict[str, Any]:
    channels = ["SMS"]
    payload: dict[str, Any] = {
        "message": "OTP sent",
        "phone": phone,
        "status": "pending_verification",
        "otp_channels": channels,
        "email_otp_sent": False,
        "email_otp_queued": False,
    }

    if email_sent:
        channels.append("EMAIL")
        payload["message"] = "OTP sent on SMS and email"
        payload["email_otp_sent"] = True
        if masked_email:
            payload["masked_email"] = masked_email
        return payload

    if email_queued:
        payload["message"] = "OTP sent on SMS. Email delivery in progress"
        payload["email_otp_queued"] = True
        if masked_email:
            payload["masked_email"] = masked_email
        return payload

    return payload

async def _send_admin_login_otp_to_telegram(*, otp_code: str, phone: str, identifier: str) -> None:
    bot_token = _clean_env_value(settings.TELEGRAM_BOT_TOKEN)
    chat_ids_raw = _resolve_admin_login_chat_id()

    if not bot_token:
        raise RuntimeError("Admin OTP bot token is missing. Set TELEGRAM_BOT_TOKEN in Railway.")
    if not chat_ids_raw:
        raise RuntimeError(
            "Admin Telegram chat ID is missing. Set ADMIN_LOGIN_TELEGRAM_CHAT_ID in Railway."
        )

    chat_ids = [cid.strip() for cid in chat_ids_raw.replace(";", ",").split(",") if cid.strip()]
    if not chat_ids:
        raise RuntimeError("No valid chat IDs found in ADMIN_LOGIN_TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text_content = (
        "GamerzAdda Admin Login OTP\n"
        f"OTP: {otp_code}\n"
        "Valid for 5 minutes.\n"
        f"Identifier: {identifier or '--'}\n"
        f"Phone: {phone}\n"
        "Do not share this code."
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        errors = []
        for chat_id in chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text_content,
                "disable_web_page_preview": True,
            }
            try:
                response = await client.post(url, json=payload)
                if response.status_code >= 400:
                    logger.error(
                        "Admin OTP Telegram send failed for %s. status=%s body=%s",
                        chat_id,
                        response.status_code,
                        (response.text or "")[:240],
                    )
                    errors.append(f"Failed for {chat_id}")
                    continue
                
                body = response.json()
                if isinstance(body, dict) and body.get("ok") is False:
                    logger.error("Admin OTP Telegram rejected by API for %s: %s", chat_id, body)
                    errors.append(f"Rejected for {chat_id}")
            except Exception as e:
                logger.error("Admin OTP Telegram send error for %s: %s", chat_id, e)
                errors.append(f"Network err for {chat_id}")

        if len(errors) >= len(chat_ids):
            raise RuntimeError("Failed to send OTP to any Telegram chat ID")


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
        deposit_balance=Decimal("0.00"),
        winning_balance=Decimal("0.00"),
        bonus_balance=Decimal("0.00"),
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
        active_phone_lock = await get_active_phone_lock_async(db, normalized_phone)
        if active_phone_lock:
            phone_restricted = True
            status = OTP_LOCK_STATUS
            message = OTP_LOCK_CLIENT_MESSAGE

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
            if status == OTP_LOCK_STATUS:
                message = message or OTP_LOCK_CLIENT_MESSAGE
            else:
                payload = _build_banned_support_response(matched_user, normalized_phone)
                status = payload.get("status") if not status else status
                message = payload.get("message") if not message else message
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

    await _raise_if_phone_otp_locked(db, phone)

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
        await register_otp_send_success_async(
            db=db,
            phone=phone,
            source="SIGNUP",
            user=None,
        )
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
    is_signup_pending = normalized_phone in _pending_signups

    phone_candidates = list(_phone_variants(normalized_phone))
    if phone_candidates:
        result = await db.execute(select(User).where(User.phone_number.in_(phone_candidates)))
    else:
        result = await db.execute(select(User).where(User.phone_number == normalized_phone))
    db_user = result.scalar_one_or_none()

    if is_admin_phone:
        is_valid, reason = _verify_admin_login_otp(phone=normalized_phone, otp_code=otp)
        if not is_valid:
            if reason in {"missing", "expired"}:
                raise HTTPException(status_code=400, detail="OTP expired or not requested. Please resend.")
            raise HTTPException(status_code=400, detail="Invalid OTP")
    else:
        verification_id = _otp_store.get(normalized_phone)
        sms_valid = False
        sms_invalid = False
        sms_provider_error = False

        if verification_id:
            from services import otp as otp_service
            try:
                sms_valid = await otp_service.verify_otp(verification_id, otp)
                sms_invalid = not sms_valid
            except Exception as e:
                sms_provider_error = True
                logger.error(f"OTP verify provider error: {e}")

        email_valid = False
        email_reason = "missing"
        email_entry: dict[str, Any] | None = None

        # Signup verification remains SMS-only. Email OTP is login-only fallback.
        if not sms_valid and not is_signup_pending:
            email_valid, email_reason, email_entry = _verify_email_login_otp(phone=normalized_phone, otp_code=otp)
            if email_valid:
                email_for_log = (db_user.email if db_user else None) or str(email_entry.get("email") or "")
                await _log_email_otp_event(
                    db,
                    user=db_user,
                    email=email_for_log,
                    phone=normalized_phone,
                    source="LOGIN",
                    event_type="VERIFY",
                    status="VERIFIED",
                    message="Verified using email OTP",
                    request=request,
                    commit=False,
                )

        if not sms_valid and not email_valid:
            if not is_signup_pending and email_entry:
                email_for_log = (db_user.email if db_user else None) or str(email_entry.get("email") or "")
                log_status = "INVALID" if email_reason == "invalid" else "EXPIRED" if email_reason == "expired" else "FAILED"
                await _log_email_otp_event(
                    db,
                    user=db_user,
                    email=email_for_log,
                    phone=normalized_phone,
                    source="LOGIN",
                    event_type="VERIFY",
                    status=log_status,
                    message="Email OTP verification failed",
                    request=request,
                    commit=True,
                )

            if sms_provider_error:
                raise HTTPException(
                    status_code=503,
                    detail="OTP verification service is temporarily unavailable. Please retry in 30 seconds."
                )

            if sms_invalid or email_reason == "invalid":
                raise HTTPException(status_code=400, detail="Invalid OTP")

            raise HTTPException(status_code=400, detail="OTP expired or not requested. Please resend.")

        _otp_store.pop(normalized_phone, None)
        _clear_email_login_otp(normalized_phone)

    signup_bonus_amount = None
    if is_signup_pending:
        pending_data = _pending_signups.pop(normalized_phone)

        ref_code = await generate_unique_referral_code_async(
            db=db,
            username=pending_data["username"],
        )

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
            wallet_balance=Decimal("0.00"),
            deposit_balance=Decimal("0.00"),
            winning_balance=Decimal("0.00"),
            bonus_balance=Decimal("0.00"),
        )

        db.add(db_user)
        await db.commit()
        await db.refresh(db_user)

        # Assign avatar
        db_user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{(db_user.id % 5) + 1}.png"
        await db.commit()
        await db.refresh(db_user)

        # ── Credit instant signup bonus for referred users ────────
        if db_user.referred_by_id:
            try:
                from core.database import SyncSessionLocal
                from services.referral_rewards import credit_signup_bonus

                sync_db = SyncSessionLocal()
                try:
                    sync_user = sync_db.query(User).filter(User.id == db_user.id).with_for_update().first()
                    if sync_user:
                        bonus = credit_signup_bonus(sync_db, sync_user)
                        if bonus:
                            sync_db.commit()
                            signup_bonus_amount = float(bonus)
                            # Refresh async session to pick up balance updates
                            await db.refresh(db_user)
                finally:
                    sync_db.close()
            except Exception as bonus_err:
                logger.error("Signup bonus credit failed for user %s: %s", db_user.id, bonus_err)
        # ──────────────────────────────────────────────────────────

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    await reset_otp_lock_after_success_async(
        db=db,
        phone=normalized_phone,
        user=db_user,
    )

    if db_user.role != "ADMIN":
        await register_login_session_success_async(db, db_user)

    client_ip = extract_client_ip(request)
    device_name = _resolve_login_device(request)

    if db_user.role != "ADMIN":
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
    response = {
        "access_token": create_access_token({"sub": str(db_user.id), "tv": token_version}),
        "token_type": "bearer",
        "role": db_user.role,
        "user": user_payload,
    }
    return response

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, login_data: LoginRequest, db: AsyncSession = Depends(get_db)) -> Any:
    raw_identifier = login_data.email.strip()
    identifier = raw_identifier.lower()
    if identifier.isdigit() and len(identifier) == 10:
        identifier = f"+91{identifier}"

    user: User | None = None
    is_admin_source_request = _is_admin_web_login_request(request)
    is_admin_identifier_attempt = _matches_admin_login_identifier(raw_identifier)

    if is_admin_source_request or is_admin_identifier_attempt:
        configured_identifier = (settings.ADMIN_LOGIN_IDENTIFIER or "").strip()
        configured_phone = _normalize_signup_phone(settings.ADMIN_LOGIN_PHONE)

        if not configured_identifier or not configured_phone:
            logger.error("Admin login blocked: ADMIN_LOGIN_IDENTIFIER/ADMIN_LOGIN_PHONE not configured")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Admin login is not configured. "
                    "Set ADMIN_LOGIN_IDENTIFIER and ADMIN_LOGIN_PHONE in Railway variables."
                ),
            )

        if not is_admin_identifier_attempt:
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
        await _raise_if_phone_otp_locked(db, user.phone_number)
        await ensure_login_session_lock_not_blocking_async(db, user)

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
        await register_otp_send_success_async(
            db=db,
            phone=user.phone_number,
            source="LOGIN",
            user=user,
        )

        email_sent, masked_email, email_queued = await _send_login_email_otp_if_available(
            request=request,
            db=db,
            user=user,
            source="LOGIN",
        )

        logger.info(f"OTP successfully sent for login: {user.phone_number}")
        return _build_login_otp_response(
            phone=user.phone_number,
            email_sent=email_sent,
            masked_email=masked_email,
            email_queued=email_queued,
        )
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

    await _raise_if_phone_otp_locked(db, normalized_phone)

    if existing_user:
        await ensure_login_session_lock_not_blocking_async(db, existing_user)

    if existing_user and await _is_blocked_for_login_support(db, existing_user):
        return _build_banned_support_response(existing_user, normalized_phone)

    if not existing_user and normalized_phone not in _pending_signups:
        raise HTTPException(status_code=404, detail="Account not found for this phone")

    try:
        from services import otp as otp_service
        res = await otp_service.send_otp(normalized_phone)
        _otp_store[normalized_phone] = res["data"]["verificationId"]
        await register_otp_send_success_async(
            db=db,
            phone=normalized_phone,
            source="RESEND",
            user=existing_user,
        )

        email_sent = False
        email_queued = False
        masked_email: str | None = None
        if existing_user:
            email_sent, masked_email, email_queued = await _send_login_email_otp_if_available(
                request=request,
                db=db,
                user=existing_user,
                source="RESEND",
            )

        return _build_login_otp_response(
            phone=normalized_phone,
            email_sent=email_sent,
            masked_email=masked_email,
            email_queued=email_queued,
        )
    except Exception as e:
        logger.error(f"OTP SEND ERR RESEND: {e}")
        raise HTTPException(status_code=503, detail=f"Failed to resend OTP: {str(e)}")
