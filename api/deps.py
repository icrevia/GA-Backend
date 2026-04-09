from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from core.database import get_db_sync
from core.config import settings
from core.security import decode_access_token
from models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False)

# Backward-compat export used by legacy modules (e.g. admin routes).
get_db = get_db_sync


def _session_revoked_detail(user: User) -> str:
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


def get_current_active_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
