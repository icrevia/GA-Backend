from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from core.database import get_db_sync, get_db as get_db_async
from core.config import settings
from core.security import decode_access_token
from models.user import User
from services.restrictions import (
    RESTRICTION_SCOPE_FULL_APP,
    RESTRICTION_SCOPE_PAGE,
    build_restriction_detail,
    get_active_restrictions_for_user,
    get_active_restrictions_for_user_async,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False)

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


def _enforce_full_app_restriction(db: Session, user: User) -> None:
    if user.role == "ADMIN":
        return

    active_full_app_restrictions = get_active_restrictions_for_user(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    if active_full_app_restrictions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=build_restriction_detail(active_full_app_restrictions[0]),
        )


def _enforce_page_restriction(db: Session, user: User, page_key: str) -> None:
    if user.role == "ADMIN":
        return

    active_page_restrictions = get_active_restrictions_for_user(
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


def get_current_user(
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

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    # ── Token version check — instant revocation ──────────────────────────────
    # When a user is banned or force-logged-out, their token_version is incremented.
    # Any token issued before that increment carries the old version and is rejected here.
    token_version = payload.get("tv", 0)
    db_token_version = getattr(user, "token_version", 0) or 0
    if int(token_version) != int(db_token_version):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_revoked_detail(user),
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_user_async(
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

    result = await db.execute(select(User).where(User.id == int(user_id)))
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
