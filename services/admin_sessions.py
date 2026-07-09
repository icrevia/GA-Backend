from datetime import datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.admin_access_session import AdminAccessSession
from models.user import User
from services.login_security import extract_client_ip


ADMIN_DEVICE_ID_HEADER = "x-admin-device-id"


def get_admin_device_id(request: Request) -> str:
    return (request.headers.get(ADMIN_DEVICE_ID_HEADER) or "").strip()


def resolve_admin_device_name(request: Request) -> str:
    preferred = (request.headers.get("x-device-name") or "").strip()
    if preferred:
        return preferred[:160]

    user_agent = (request.headers.get("user-agent") or "").strip()
    if not user_agent:
        return "Unknown Device"

    lowered = user_agent.lower()

    browser = "Browser"
    if "edg/" in lowered:
        browser = "Microsoft Edge"
    elif "opr/" in lowered or "opera/" in lowered:
        browser = "Opera"
    elif "chrome/" in lowered and "edg/" not in lowered and "opr/" not in lowered:
        browser = "Google Chrome"
    elif "firefox/" in lowered:
        browser = "Mozilla Firefox"
    elif "safari/" in lowered and "chrome/" not in lowered and "chromium/" not in lowered:
        browser = "Safari"
    elif "trident/" in lowered or "msie" in lowered:
        browser = "Internet Explorer"

    os_name = "Unknown OS"
    if "windows nt" in lowered:
        os_name = "Windows"
    elif "android" in lowered:
        os_name = "Android"
    elif "iphone" in lowered or "ipad" in lowered or "ipod" in lowered:
        os_name = "iOS"
    elif "macintosh" in lowered or "mac os x" in lowered:
        os_name = "macOS"
    elif "linux" in lowered:
        os_name = "Linux"

    return f"{browser} on {os_name}"[:160]


def _invalid_session_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _apply_session_metadata(session: AdminAccessSession, user: User, request: Request, *, refreshed: bool) -> None:
    now = datetime.utcnow()
    session.user_id = user.id
    session.device_name = resolve_admin_device_name(request)
    session.user_agent = (request.headers.get("user-agent") or "").strip()[:255] or None
    session.ip_address = (extract_client_ip(request) or "").strip()[:64] or None
    session.is_active = True
    session.revoked_at = None
    session.revoked_reason = None
    session.last_seen_at = now
    if refreshed:
        session.created_at = now


def ensure_admin_access_session_sync(db: Session, user: User, request: Request) -> AdminAccessSession | None:
    device_id = get_admin_device_id(request)
    if not device_id:
        return None

    session = db.query(AdminAccessSession).filter(AdminAccessSession.device_id == device_id).first()
    if session:
        if session.user_id != user.id or not session.is_active:
            raise _invalid_session_error()
        return session

    session = AdminAccessSession(device_id=device_id)
    _apply_session_metadata(session, user, request, refreshed=True)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


async def ensure_admin_access_session_async(
    db: AsyncSession,
    user: User,
    request: Request,
) -> AdminAccessSession | None:
    device_id = get_admin_device_id(request)
    if not device_id:
        return None

    result = await db.execute(select(AdminAccessSession).where(AdminAccessSession.device_id == device_id))
    session = result.scalar_one_or_none()
    if session:
        if session.user_id != user.id or not session.is_active:
            raise _invalid_session_error()
        return session

    session = AdminAccessSession(device_id=device_id)
    _apply_session_metadata(session, user, request, refreshed=True)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


def upsert_admin_access_session_sync(db: Session, user: User, request: Request) -> AdminAccessSession | None:
    device_id = get_admin_device_id(request)
    if not device_id:
        return None

    session = db.query(AdminAccessSession).filter(AdminAccessSession.device_id == device_id).first()
    if session is None:
        session = AdminAccessSession(device_id=device_id)
        db.add(session)

    _apply_session_metadata(session, user, request, refreshed=True)
    return session


async def upsert_admin_access_session_async(db: AsyncSession, user: User, request: Request) -> AdminAccessSession | None:
    device_id = get_admin_device_id(request)
    if not device_id:
        return None

    result = await db.execute(select(AdminAccessSession).where(AdminAccessSession.device_id == device_id))
    session = result.scalar_one_or_none()
    if session is None:
        session = AdminAccessSession(device_id=device_id)
        db.add(session)

    _apply_session_metadata(session, user, request, refreshed=True)
    return session