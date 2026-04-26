from datetime import datetime
import time
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from core.database import get_db_sync, get_db as get_db_async
from core.config import settings
from core.security import decode_access_token
from services.admin_sessions import ensure_admin_access_session_async, ensure_admin_access_session_sync
from models.user import User
from models.config import SystemConfig
from services.restrictions import (
    RESTRICTION_SCOPE_FULL_APP,
    RESTRICTION_SCOPE_PAGE,
    build_restriction_detail,
    get_active_restrictions_for_user,
    get_active_restrictions_for_user_async,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False)

MAINTENANCE_CACHE_TTL_SECONDS = 5.0
_maintenance_guard_cache: dict[str, object] = {
    "expires_at": 0.0,
    "enabled": False,
    "message": "",
    "until": "",
}

# Backward-compat export used by legacy modules (e.g. admin routes).
get_db = get_db_sync


def _session_revoked_detail(user: User) -> str:
    if not bool(getattr(user, "is_active", True)):
        return (
            "Session ended because your account is restricted by admin. "
            "Please contact support via Live Chat."
        )

    base = "Session ended because your account was logged in from another device."
    device = (getattr(user, "last_login_device", None) or "").strip()
    ip = (getattr(user, "last_login_ip", None) or "").strip()

    if device and ip:
        return f"{base} New login: {device} (IP: {ip}). Please log in again."
    if device:
        return f"{base} New login: {device}. Please log in again."
    if ip:
        return f"{base} New login IP: {ip}. Please log in again."
    return f"{base} Please log in again."


def _maintenance_enabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_maintenance_config(config_map: dict[str, str]) -> tuple[bool, str, str]:
    enabled = _maintenance_enabled(config_map.get("maintenance_mode"))
    message = (config_map.get("maintenance_message") or "").strip()
    until = (config_map.get("maintenance_until") or "").strip()
    return enabled, message, until


def _set_maintenance_cache(enabled: bool, message: str, until: str) -> tuple[bool, str, str]:
    _maintenance_guard_cache["enabled"] = enabled
    _maintenance_guard_cache["message"] = message
    _maintenance_guard_cache["until"] = until
    _maintenance_guard_cache["expires_at"] = time.monotonic() + MAINTENANCE_CACHE_TTL_SECONDS
    return enabled, message, until


def _get_maintenance_state_sync(db: Session) -> tuple[bool, str, str]:
    now = time.monotonic()
    if now < float(_maintenance_guard_cache.get("expires_at", 0.0) or 0.0):
        return (
            bool(_maintenance_guard_cache.get("enabled", False)),
            str(_maintenance_guard_cache.get("message", "") or ""),
            str(_maintenance_guard_cache.get("until", "") or ""),
        )

    rows = (
        db.query(SystemConfig.config_key, SystemConfig.config_value)
        .filter(SystemConfig.config_key.in_(["maintenance_mode", "maintenance_message", "maintenance_until"]))
        .all()
    )
    config_map = {k: v for k, v in rows}
    enabled, message, until = _parse_maintenance_config(config_map)
    return _set_maintenance_cache(enabled, message, until)


async def _get_maintenance_state_async(db: AsyncSession) -> tuple[bool, str, str]:
    now = time.monotonic()
    if now < float(_maintenance_guard_cache.get("expires_at", 0.0) or 0.0):
        return (
            bool(_maintenance_guard_cache.get("enabled", False)),
            str(_maintenance_guard_cache.get("message", "") or ""),
            str(_maintenance_guard_cache.get("until", "") or ""),
        )

    result = await db.execute(
        select(SystemConfig.config_key, SystemConfig.config_value).where(
            SystemConfig.config_key.in_(["maintenance_mode", "maintenance_message", "maintenance_until"])
        )
    )
    config_map = {k: v for k, v in result.all()}
    enabled, message, until = _parse_maintenance_config(config_map)
    return _set_maintenance_cache(enabled, message, until)


def _maintenance_client_message(message: str) -> str:
    cleaned = (message or "").strip()
    return cleaned or "System is under maintenance. Please try again shortly."


def _enforce_maintenance_guard(db: Session, user: User) -> None:
    if user.role == "ADMIN":
        return

    enabled, message, _ = _get_maintenance_state_sync(db)
    if enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_maintenance_client_message(message),
        )


async def _enforce_maintenance_guard_async(db: AsyncSession, user: User) -> None:
    if user.role == "ADMIN":
        return

    enabled, message, _ = await _get_maintenance_state_async(db)
    if enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_maintenance_client_message(message),
        )


def _enforce_full_app_restriction(db: Session, user: User) -> None:
    if user.role == "ADMIN":
        return

    # Use pre-loaded restrictions from memory (joinedload)
    from services.restrictions import is_restriction_currently_active, RESTRICTION_SCOPE_FULL_APP
    now_value = datetime.utcnow()
    
    active_full_app_restrictions = [
        r for r in getattr(user, "restrictions", [])
        if r.scope == RESTRICTION_SCOPE_FULL_APP and is_restriction_currently_active(r, now_value)
    ]

    if active_full_app_restrictions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=build_restriction_detail(active_full_app_restrictions[0]),
        )


def _enforce_page_restriction(db: Session, user: User, page_key: str) -> None:
    if user.role == "ADMIN":
        return

    # Use pre-loaded restrictions from memory (joinedload)
    from services.restrictions import is_restriction_currently_active, RESTRICTION_SCOPE_PAGE, normalize_restriction_page_key
    now_value = datetime.utcnow()
    normalized_key = normalize_restriction_page_key(page_key)
    
    active_page_restrictions = [
        r for r in getattr(user, "restrictions", [])
        if r.scope == RESTRICTION_SCOPE_PAGE 
        and normalize_restriction_page_key(r.page_key) == normalized_key 
        and is_restriction_currently_active(r, now_value)
    ]

    if active_page_restrictions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=build_restriction_detail(active_page_restrictions[0]),
        )


async def _enforce_full_app_restriction_async(db: AsyncSession, user: User) -> None:
    if user.role == "ADMIN":
        return

    active_full_app_restrictions = await get_active_restrictions_for_user_async(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    if active_full_app_restrictions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=build_restriction_detail(active_full_app_restrictions[0]),
        )


async def _enforce_page_restriction_async(db: AsyncSession, user: User, page_key: str) -> None:
    if user.role == "ADMIN":
        return

    active_page_restrictions = await get_active_restrictions_for_user_async(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_PAGE,
        page_key=page_key,
    )
    if active_page_restrictions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=build_restriction_detail(active_page_restrictions[0]),
        )


from sqlalchemy.orm import Session, joinedload

def get_current_user(
    request: Request,
    db: Session = Depends(get_db_sync),
    token: str | None = Depends(oauth2_scheme),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token)

    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    # Optimization: Eagerly load active restrictions to avoid extra DB roundtrips later.
    user = (
        db.query(User)
        .options(joinedload(User.restrictions))
        .filter(User.id == int(user_id))
        .first()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    # ── Token version check — instant revocation ──────────────────────────────
    token_version = payload.get("tv", 0)
    db_token_version = getattr(user, "token_version", 0) or 0
    if int(token_version) != int(db_token_version):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_revoked_detail(user),
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.role == "ADMIN":
        ensure_admin_access_session_sync(db, user, request)

    _enforce_maintenance_guard(db, user)

    return user


async def get_current_user_async(
    request: Request,
    db: AsyncSession = Depends(get_db_async),
    token: str | None = Depends(oauth2_scheme),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token)
    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    # Optimization: Eagerly load active restrictions to avoid extra DB roundtrips later.
    from sqlalchemy.orm import selectinload
    stmt = (
        select(User)
        .options(selectinload(User.restrictions))
        .where(User.id == int(user_id))
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    token_version = payload.get("tv", 0)
    db_token_version = getattr(user, "token_version", 0) or 0
    if int(token_version) != int(db_token_version):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_revoked_detail(user),
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.role == "ADMIN":
        await ensure_admin_access_session_async(db, user, request)

    await _enforce_maintenance_guard_async(db, user)

    return user


def get_user_for_support(
    db: Session = Depends(get_db_sync),
    token: str | None = Depends(oauth2_scheme),
) -> User:
    """Special dependency for support — allows users to connect even if restricted/banned."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Token version check — must still match current user version
    token_version = payload.get("tv", 0)
    db_token_version = getattr(user, "token_version", 0) or 0
    if int(token_version) != int(db_token_version):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_revoked_detail(user)
        )

    return user


async def get_user_for_support_async(
    db: AsyncSession = Depends(get_db_async),
    token: str | None = Depends(oauth2_scheme),
) -> User:
    """Special dependency for support — allows users to connect even if restricted/banned."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    token_version = payload.get("tv", 0)
    db_token_version = getattr(user, "token_version", 0) or 0
    if int(token_version) != int(db_token_version):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_revoked_detail(user)
        )

    return user


def get_current_active_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def get_current_user_wallet(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
) -> User:
    _enforce_full_app_restriction(db, current_user)
    _enforce_page_restriction(db, current_user, "WALLET")
    return current_user


def get_current_user_tournaments(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
) -> User:
    _enforce_full_app_restriction(db, current_user)
    _enforce_page_restriction(db, current_user, "TOURNAMENTS")
    return current_user


def get_current_user_referral(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
) -> User:
    _enforce_full_app_restriction(db, current_user)
    _enforce_page_restriction(db, current_user, "REFERRAL")
    return current_user


def get_current_user_quizzes(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
) -> User:
    _enforce_full_app_restriction(db, current_user)
    _enforce_page_restriction(db, current_user, "QUIZ")
    return current_user


def get_current_user_profile(
    db: Session = Depends(get_db_sync),
    current_user: User = Depends(get_current_user),
) -> User:
    _enforce_full_app_restriction(db, current_user)
    _enforce_page_restriction(db, current_user, "PROFILE")
    return current_user


async def get_current_user_profile_async(
    db: AsyncSession = Depends(get_db_async),
    current_user: User = Depends(get_current_user_async),
) -> User:
    await _enforce_full_app_restriction_async(db, current_user)
    await _enforce_page_restriction_async(db, current_user, "PROFILE")
    return current_user
