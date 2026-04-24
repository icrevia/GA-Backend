from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, BackgroundTasks, Request, Form
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import DataError
from typing import List
from decimal import Decimal, ROUND_HALF_UP
from schemas.user import UserResponse, SubAdminCreate, SubAdminUpdate
import uuid
import os
import uuid
import logging
import hashlib
import json
import secrets
import io
from PIL import Image
from threading import Lock
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib import request as urllib_request
import random
import string

from api.deps import get_db, get_current_active_admin
from core.config import settings
from core.security import hash_password
from models.user import User
from models.admin_access_session import AdminAccessSession
from models.banner import HomeBanner
from models.promo import PromoCode
from models.otp_phone_lock import OtpPhoneLock
from models.user_activity_lock import UserActivityLock
from models.restriction import UserRestriction
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.config import SystemConfig, HomePopup
from models.notification import Notification
from models.participant import TournamentParticipant
from models.support import ChatSession, ChatMessage
from models.withdraw_upi_account import WithdrawUpiAccount
from services.notifications import add_user_notification
from services.push_notifications import send_push, send_push_to_many, send_push_to_many_detailed
from services.notification_text import append_firebase_suffix
from services.restrictions import (
    RESTRICTION_SCOPE_FULL_APP,
    RESTRICTION_SCOPE_PAGE,
    VALID_RESTRICTION_PAGE_KEYS,
    build_restriction_detail,
    get_active_restrictions_for_user,
    is_restriction_currently_active,
    normalize_restriction_page_key,
    normalize_restriction_scope,
    serialize_user_restriction,
    to_naive,
    utcnow_naive,
)
from services.match_stats import (
    classify_game_mode,
    compute_match_stats_for_user,
    compute_match_stats_for_user_ids,
    empty_user_match_stats,
    leaderboard_prize_payment_mode,
)
from services.otp_limits import (
    clear_otp_lock_for_user_sync,
    list_otp_locks_sync,
    reset_otp_lock_sync,
)
from services.activity_limits import (
    clear_activity_locks_for_user_sync,
    list_activity_locks_sync,
    reset_activity_lock_sync,
)
from core.websockets import manager as ws_manager
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    InsufficientWalletBalanceError,
    credit_wallet,
    debit_wallet,
    ensure_wallet_buckets,
    sync_wallet_total,
    get_total_balance,
    to_money,
)
from services.deposit_bonus import (
    apply_deposit_bonus_if_eligible,
    get_deposit_bonus_config,
    set_deposit_bonus_config,
)
from services.referral_rewards import (
    REFERRAL_REWARD_TX_TYPE,
    get_referral_reward_config,
    set_referral_reward_config,
)

from schemas.admin import (
    SystemConfigUpdate,
    DepositBonusConfigUpdate,
    DepositBonusConfigResponse,
    ReferralRewardConfigUpdate,
    ReferralRewardConfigResponse,
    NotificationSendRequest,
    UserStatusUpdate,
    RestrictionCreateRequest,
    BulkRestrictionCreateRequest,
    RestrictionUnlockRequest,
    OtpLockResetRequest,
    ActivityLockResetRequest,
    AdminWalletTransactionResponse,
    UserWalletBucketsUpdate,
    TournamentRoomUpdate,
    TournamentConclude,
    TournamentCreateAdmin,
    TournamentUpdateAdmin,
    DeveloperOtpRequestResponse,
    DeveloperOtpVerifyRequest,
    DeveloperOtpVerifyResponse,
    DeveloperOtpStatusResponse,
    AdminAccessSessionResponse,
    PromoCreateRequest,
    PromoUpdateRequest,
    BannerCreateRequest,
    BannerUpdateRequest,
    KillRewardEntry,
    HomePopupCreateRequest,
    HomePopupResponse,
)
from schemas.tournament import TournamentCreate, TournamentResponse, TournamentSlotsBoardResponse
from services.admin_sessions import get_admin_device_id

logger = logging.getLogger("GamerzAdda.admin")
router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# APK Upload — FIXED: path traversal prevention + size cap
# ─────────────────────────────────────────────────────────────────

MAX_APK_SIZE_MB = 150
MAX_APK_SIZE_BYTES = MAX_APK_SIZE_MB * 1024 * 1024
MAX_NUMERIC_12_2 = Decimal("9999999999.99")

_DEVELOPER_OTP_LOCK = Lock()
_DEVELOPER_OTP_STATE: dict[int, dict[str, object]] = {}
_DEVELOPER_OTP_SESSIONS: dict[str, tuple[int, datetime]] = {}


def _save_apk_upload_file(file: UploadFile, *, admin_username: str) -> tuple[str, str, int]:
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="Only APK files are allowed.")

    safe_filename = f"GamerzAdda_app_{uuid.uuid4().hex}.apk"
    static_dir = "static"
    os.makedirs(static_dir, exist_ok=True)
    file_path = os.path.join(static_dir, safe_filename)

    try:
        written = 0
        with open(file_path, "wb") as buffer:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_APK_SIZE_BYTES:
                    os.remove(file_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"APK file exceeds {MAX_APK_SIZE_MB} MB size limit.",
                    )
                buffer.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("APK upload failed: %s", e)
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail="Failed to save file.")

    logger.info(
        "APK uploaded by admin=%s: %s (%.1f MB)",
        admin_username,
        safe_filename,
        written / (1024 * 1024),
    )
    return f"/static/{safe_filename}", safe_filename, written


def _upsert_system_config_value(db: Session, key: str, value: str) -> None:
    config = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if not config:
        config = SystemConfig(config_key=key, config_value=value)
        db.add(config)
    else:
        config.config_value = value


def _resolve_public_base_url(request: Request) -> str:
    configured = (settings.APP_URL or "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _parse_telegram_chat_ids(raw_value: str | None) -> list[str]:
    normalized = (raw_value or "").replace("\n", ",").replace(";", ",")
    chat_ids: list[str] = []
    for chunk in normalized.split(","):
        chat_id = chunk.strip()
        if not chat_id:
            continue
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
    return chat_ids


def _developer_otp_chat_ids() -> list[str]:
    configured = _parse_telegram_chat_ids(settings.DEVELOPER_OTP_TELEGRAM_CHAT_ID)
    if configured:
        return configured
    return _parse_telegram_chat_ids(settings.TELEGRAM_ALERT_CHAT_ID)


def _otp_digest(admin_id: int, otp: str) -> str:
    payload = f"{settings.SECRET_KEY}|developer-otp|{admin_id}|{otp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generate_numeric_otp(length: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _cleanup_developer_otp_state(now: datetime) -> None:
    for admin_id, state in list(_DEVELOPER_OTP_STATE.items()):
        expires_at = state.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at <= now:
            _DEVELOPER_OTP_STATE.pop(admin_id, None)

    for token, (admin_id, expires_at) in list(_DEVELOPER_OTP_SESSIONS.items()):
        if expires_at <= now:
            _DEVELOPER_OTP_SESSIONS.pop(token, None)


def _validate_developer_otp_session(admin_id: int, otp_session_token: str) -> tuple[bool, int]:
    if not otp_session_token:
        return False, 0

    now = datetime.utcnow()
    with _DEVELOPER_OTP_LOCK:
        _cleanup_developer_otp_state(now)
        record = _DEVELOPER_OTP_SESSIONS.get(otp_session_token)
        if not record:
            return False, 0

        token_admin_id, expires_at = record
        if token_admin_id != admin_id:
            return False, 0

        if expires_at <= now:
            _DEVELOPER_OTP_SESSIONS.pop(otp_session_token, None)
            return False, 0

        remaining = int((expires_at - now).total_seconds())
        return True, max(1, remaining)


def _send_developer_otp_message(admin: User, request: Request, otp: str) -> None:
    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    chat_ids = _developer_otp_chat_ids()
    if not bot_token or not chat_ids:
        raise HTTPException(
            status_code=503,
            detail="Developer OTP delivery is not configured. Set TELEGRAM_BOT_TOKEN and DEVELOPER_OTP_TELEGRAM_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID).",
        )

    client_ip = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    client_ip = client_ip.split(",")[0].strip() if client_ip else "unknown"

    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    message = "\n".join([
        "=== GamerzAdda Developer OTP ===",
        f"Admin ID: {admin.id}",
        f"Username: {admin.username}",
        f"OTP: {otp}",
        f"Valid For (seconds): {settings.DEVELOPER_OTP_TTL_SECONDS}",
        f"Requested At (UTC): {now_utc}",
        f"Request IP: {client_ip}",
        "Never share this code with anyone.",
    ])[:4096]

    delivered_count = 0
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }

        req = urllib_request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=settings.SECURITY_ALERT_TIMEOUT_SECONDS) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                parsed = json.loads(body) if body else {}
                if resp.status >= 400 or (isinstance(parsed, dict) and parsed.get("ok") is False):
                    logger.warning(
                        "Developer OTP telegram send failed: chat_id=%s status=%s body=%s",
                        chat_id,
                        resp.status,
                        body,
                    )
                    continue
                delivered_count += 1
        except HTTPError as exc:
            logger.warning("Developer OTP telegram HTTPError: chat_id=%s status=%s", chat_id, exc.code)
        except Exception as exc:
            logger.warning("Developer OTP telegram error for chat_id=%s: %s", chat_id, exc)

    if delivered_count <= 0:
        raise HTTPException(status_code=503, detail="Failed to deliver OTP. Please retry.")

    if delivered_count < len(chat_ids):
        logger.warning(
            "Developer OTP delivered partially: delivered=%s total=%s",
            delivered_count,
            len(chat_ids),
        )


def require_developer_otp(
    request: Request,
    current_user: User = Depends(get_current_active_admin),
) -> User:
    if not settings.DEVELOPER_OTP_ENABLED:
        return current_user

    otp_session_token = (request.headers.get("x-developer-otp-token") or "").strip()
    verified, _ = _validate_developer_otp_session(current_user.id, otp_session_token)
    if not verified:
        raise HTTPException(status_code=401, detail="Developer OTP verification required")

    return current_user


def _serialize_admin_access_session(session: AdminAccessSession, current_device_id: str | None) -> dict:
    user = session.user
    return {
        "id": session.id,
        "user_id": session.user_id,
        "username": user.username,
        "email": user.email,
        "phone_number": user.phone_number,
        "device_id": session.device_id,
        "device_name": session.device_name,
        "user_agent": session.user_agent,
        "ip_address": session.ip_address,
        "is_active": bool(session.is_active),
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "revoked_at": session.revoked_at,
        "revoked_reason": session.revoked_reason,
        "is_current_admin": bool(current_device_id and session.device_id == current_device_id),
        "access_enabled": bool(session.is_active),
    }


@router.get("/developer/admin-access/sessions", response_model=list[AdminAccessSessionResponse])
def list_developer_admin_access_sessions(
    request: Request,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    current_device_id = get_admin_device_id(request)
    sessions_q = db.query(AdminAccessSession).join(User).filter(User.role == "ADMIN")
    if not include_inactive:
        sessions_q = sessions_q.filter(User.is_active.is_(True), AdminAccessSession.is_active.is_(True))

    sessions = sessions_q.order_by(AdminAccessSession.created_at.desc(), AdminAccessSession.id.desc()).all()

    return [
        _serialize_admin_access_session(session, current_device_id)
        for session in sessions
    ]


@router.post("/developer/admin-access/sessions/{session_id}/logout")
def logout_developer_admin_access_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    current_device_id = get_admin_device_id(request)
    target_session = (
        db.query(AdminAccessSession)
        .join(User)
        .filter(AdminAccessSession.id == session_id, User.role == "ADMIN")
        .first()
    )
    if not target_session:
        raise HTTPException(status_code=404, detail="Admin session not found")

    target_session.is_active = False
    target_session.revoked_at = datetime.utcnow()
    target_session.revoked_reason = "manual_logout"
    db.add(target_session)
    db.commit()

    logger.info(
        "Developer forced admin session logout: actor_admin=%s target_session=%s target_device_id=%s",
        current_user.username,
        target_session.id,
        target_session.device_id,
    )

    if current_device_id and target_session.device_id == current_device_id:
        message = "Current admin session revoked. Please login again."
    else:
        message = f"Revoked admin session on {target_session.device_name or 'Unknown device'}."

    return {
        "message": message,
        "session_id": target_session.id,
        "device_id": target_session.device_id,
        "is_current_admin": bool(current_device_id and target_session.device_id == current_device_id),
    }


@router.post("/developer/admin-access/sessions/logout-all")
def logout_all_developer_admin_access_sessions(
    request: Request,
    include_self: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    current_device_id = get_admin_device_id(request)
    sessions_q = db.query(AdminAccessSession).join(User).filter(User.role == "ADMIN", AdminAccessSession.is_active.is_(True))
    if not include_self and current_device_id:
        sessions_q = sessions_q.filter(AdminAccessSession.device_id != current_device_id)

    sessions = sessions_q.all()
    revoked_count = 0
    for session in sessions:
        session.is_active = False
        session.revoked_at = datetime.utcnow()
        session.revoked_reason = "bulk_logout"
        db.add(session)
        revoked_count += 1

    db.commit()

    logger.info(
        "Developer forced bulk admin session logout: actor_admin=%s include_self=%s revoked=%s",
        current_user.username,
        include_self,
        revoked_count,
    )

    return {
        "message": f"Revoked sessions for {revoked_count} admin account(s).",
        "revoked_count": revoked_count,
        "include_self": include_self,
    }


@router.post("/developer/otp/request", response_model=DeveloperOtpRequestResponse)
def request_developer_otp(
    request: Request,
    current_user: User = Depends(get_current_active_admin),
):
    if not settings.DEVELOPER_OTP_ENABLED:
        return DeveloperOtpRequestResponse(
            otp_required=False,
            message="Developer OTP is disabled",
        )

    now = datetime.utcnow()
    with _DEVELOPER_OTP_LOCK:
        _cleanup_developer_otp_state(now)
        existing_state = _DEVELOPER_OTP_STATE.get(current_user.id)
        if existing_state:
            resend_after = existing_state.get("resend_after")
            if isinstance(resend_after, datetime) and resend_after > now:
                retry_after = int((resend_after - now).total_seconds())
                raise HTTPException(
                    status_code=429,
                    detail=f"Please wait {max(1, retry_after)} seconds before requesting a new OTP",
                )

        otp = _generate_numeric_otp(settings.DEVELOPER_OTP_LENGTH)
        expires_at = now + timedelta(seconds=settings.DEVELOPER_OTP_TTL_SECONDS)
        resend_after = now + timedelta(seconds=settings.DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS)
        _DEVELOPER_OTP_STATE[current_user.id] = {
            "otp_digest": _otp_digest(current_user.id, otp),
            "expires_at": expires_at,
            "attempts_left": settings.DEVELOPER_OTP_MAX_VERIFY_ATTEMPTS,
            "resend_after": resend_after,
        }

    try:
        _send_developer_otp_message(current_user, request, otp)
    except HTTPException:
        with _DEVELOPER_OTP_LOCK:
            _DEVELOPER_OTP_STATE.pop(current_user.id, None)
        raise

    logger.info("Developer OTP requested by admin_id=%s", current_user.id)
    return DeveloperOtpRequestResponse(
        otp_required=True,
        message="Developer OTP sent to Telegram",
        expires_in_seconds=settings.DEVELOPER_OTP_TTL_SECONDS,
        resend_cooldown_seconds=settings.DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS,
    )


@router.post("/developer/otp/verify", response_model=DeveloperOtpVerifyResponse)
def verify_developer_otp(
    data: DeveloperOtpVerifyRequest,
    current_user: User = Depends(get_current_active_admin),
):
    if not settings.DEVELOPER_OTP_ENABLED:
        return DeveloperOtpVerifyResponse(
            verified=True,
            message="Developer OTP is disabled",
        )

    now = datetime.utcnow()
    with _DEVELOPER_OTP_LOCK:
        _cleanup_developer_otp_state(now)
        state = _DEVELOPER_OTP_STATE.get(current_user.id)
        if not state:
            raise HTTPException(status_code=400, detail="OTP not requested or expired")

        expires_at = state.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            _DEVELOPER_OTP_STATE.pop(current_user.id, None)
            raise HTTPException(status_code=400, detail="OTP expired. Request a new OTP")

        expected_digest = state.get("otp_digest")
        if expected_digest != _otp_digest(current_user.id, data.otp):
            attempts_left = int(state.get("attempts_left", 0)) - 1
            if attempts_left <= 0:
                _DEVELOPER_OTP_STATE.pop(current_user.id, None)
                raise HTTPException(status_code=401, detail="Invalid OTP. No attempts left")

            state["attempts_left"] = attempts_left
            _DEVELOPER_OTP_STATE[current_user.id] = state
            raise HTTPException(status_code=401, detail=f"Invalid OTP. Attempts left: {attempts_left}")

        _DEVELOPER_OTP_STATE.pop(current_user.id, None)

        session_token = secrets.token_urlsafe(32)
        session_expires_at = now + timedelta(seconds=settings.DEVELOPER_OTP_SESSION_TTL_SECONDS)
        _DEVELOPER_OTP_SESSIONS[session_token] = (current_user.id, session_expires_at)

    logger.info("Developer OTP verified for admin_id=%s", current_user.id)
    return DeveloperOtpVerifyResponse(
        verified=True,
        developer_otp_token=session_token,
        expires_in_seconds=settings.DEVELOPER_OTP_SESSION_TTL_SECONDS,
        message="Developer access verified",
    )


@router.get("/developer/otp/status", response_model=DeveloperOtpStatusResponse)
def developer_otp_status(
    request: Request,
    current_user: User = Depends(get_current_active_admin),
):
    if not settings.DEVELOPER_OTP_ENABLED:
        return DeveloperOtpStatusResponse(
            otp_required=False,
            verified=True,
            expires_in_seconds=0,
        )

    otp_session_token = (request.headers.get("x-developer-otp-token") or "").strip()
    verified, ttl = _validate_developer_otp_session(current_user.id, otp_session_token)
    return DeveloperOtpStatusResponse(
        otp_required=True,
        verified=verified,
        expires_in_seconds=ttl,
    )

@router.post("/config/upload-apk")
def upload_apk(
    file: UploadFile = File(...),
    admin: User = Depends(get_current_active_admin)
):
    """Upload an APK to the static directory for OTA updates."""
    static_url, safe_filename, _written = _save_apk_upload_file(file, admin_username=admin.username)
    return {"url": static_url, "filename": safe_filename}


@router.post("/developer/config/upload-apk")
def upload_apk_developer(
    file: UploadFile = File(...),
    admin: User = Depends(require_developer_otp),
):
    return upload_apk(file=file, admin=admin)


@router.post("/developer/release/publish")
def publish_developer_release(
    request: Request,
    latest_version_code: str = Form(...),
    latest_version_name: str = Form(...),
    force_update: bool = Form(False),
    update_message: str = Form(""),
    update_url: str = Form(""),
    file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    code_raw = (latest_version_code or "").strip()
    if not code_raw.isdigit() or int(code_raw) <= 0:
        raise HTTPException(status_code=422, detail="Version code must be a positive integer.")

    name = (latest_version_name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Version name is required.")

    notes = (update_message or "").strip()
    manual_update_url = (update_url or "").strip()
    uploaded_static_url = ""
    uploaded_filename = ""
    uploaded_size_bytes = 0

    if file is not None and (file.filename or "").strip():
        uploaded_static_url, uploaded_filename, uploaded_size_bytes = _save_apk_upload_file(
            file,
            admin_username=current_user.username,
        )

    resolved_update_url = manual_update_url
    if uploaded_static_url:
        resolved_update_url = f"{_resolve_public_base_url(request)}{uploaded_static_url}"

    if not resolved_update_url:
        raise HTTPException(
            status_code=422,
            detail="Provide APK download URL or upload an APK file.",
        )

    try:
        _upsert_system_config_value(db, "latest_version_code", str(int(code_raw)))
        _upsert_system_config_value(db, "latest_version_name", name)
        _upsert_system_config_value(db, "force_update", "true" if force_update else "false")
        _upsert_system_config_value(db, "update_message", notes)
        _upsert_system_config_value(db, "update_url", resolved_update_url)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Release publish failed for admin=%s: %s", current_user.username, exc)
        raise HTTPException(status_code=500, detail="Failed to publish release")

    logger.info(
        "Developer release published by admin=%s version_code=%s version_name=%s force=%s update_url=%s",
        current_user.username,
        int(code_raw),
        name,
        force_update,
        resolved_update_url,
    )

    response_payload = {
        "message": "Release published successfully",
        "latest_version_code": int(code_raw),
        "latest_version_name": name,
        "force_update": force_update,
        "update_message": notes,
        "update_url": resolved_update_url,
    }
    if uploaded_static_url:
        response_payload["uploaded_file"] = {
            "url": uploaded_static_url,
            "filename": uploaded_filename,
            "size_bytes": uploaded_size_bytes,
        }

    return response_payload


# ─────────────────────────────────────────────────────────────────
# Tournament management
# ─────────────────────────────────────────────────────────────────

@router.get("/tournaments", response_model=List[TournamentResponse])
def list_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from api.tournaments import _with_count
    
    joined_subq = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(func.distinct(TournamentParticipant.slot_no)).label('j_count')
        )
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )

    rows = (
        db.query(Tournament, func.coalesce(joined_subq.c.j_count, 0))
        .outerjoin(joined_subq, Tournament.id == joined_subq.c.tournament_id)
        .order_by(Tournament.created_at.desc())
        .all()
    )

    result = []
    for t, count in rows:
        t.joined_count = count
        result.append(t)
    return result


@router.post("/tournaments", response_model=TournamentResponse)
def create_tournament(
    data: TournamentCreateAdmin,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from datetime import datetime
    from api.tournaments import _with_count
    try:
        dt = datetime.fromisoformat(data.match_time.replace('Z', '+00:00'))
    except Exception:
        dt = datetime.now()

    db_obj = Tournament(
        title=data.title,
        game_name=data.game_name,
        entry_fee=data.entry_fee,
        prize_pool=data.prize_pool,
        per_kill_prize=data.per_kill_prize,
        commission_percentage=data.commission_percentage,
        match_type=data.match_type,
        match_time=dt,
        map_name=data.map_name,
        game_image_url=data.game_image_url,
        max_slots=data.max_slots or 100,
        prize_distribution=data.prize_distribution,
        status="UPCOMING"
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return _with_count(db_obj, db)

@router.put("/tournaments/{tournament_id}", response_model=TournamentResponse)
def update_tournament(
    tournament_id: int,
    data: TournamentUpdateAdmin,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from datetime import datetime
    from api.tournaments import _with_count
    
    db_obj = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not db_obj:
        raise HTTPException(status_code=404, detail="Tournament not found")

    # Update simple fields
    update_data = data.model_dump(exclude_unset=True)
    
    if "match_time" in update_data and update_data["match_time"]:
        try:
            update_data["match_time"] = datetime.fromisoformat(update_data["match_time"].replace('Z', '+00:00'))
        except Exception:
            del update_data["match_time"]

    for field, value in update_data.items():
        setattr(db_obj, field, value)

    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return _with_count(db_obj, db)


@router.put("/tournaments/{tournament_id}/room", response_model=TournamentResponse)
@router.post("/tournaments/{tournament_id}/set-room", response_model=TournamentResponse)
def set_tournament_room(
    tournament_id: int,
    data: TournamentRoomUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not db_obj:
        raise HTTPException(status_code=404, detail="Tournament not found")

    db_obj.room_id       = data.room_id
    db_obj.room_password = data.room_password
    db_obj.status        = "LIVE"
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)

    # BROADCAST TO ALL PARTICIPANTS
    try:
        parts = db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).all()
        for p in parts:
            add_user_notification(
                db, p.user_id,
                "MATCH IS LIVE! 🚀",
                f"Room ID: {data.room_id} | Pass: {data.room_password} for '{db_obj.title}'. Join quickly!",
                "APP"
            )
    except Exception: pass

    return db_obj


@router.delete("/tournaments/{tournament_id}")
def delete_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).delete()
    db.delete(tournament)
    db.commit()
    return {"message": "Tournament deleted successfully"}


# ─────────────────────────────────────────────────────────────────
# Conclude tournament — FIXED: winner must be a participant
# ─────────────────────────────────────────────────────────────────

@router.post("/tournaments/{tournament_id}/conclude")
def conclude_tournament(
    tournament_id: int,
    data: TournamentConclude,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")

    per_kill_prize = to_money(getattr(tournament, 'per_kill_prize', 0.0))
    leaderboard_category = classify_game_mode(getattr(tournament, "game_name", None))
    payout_payment_mode = leaderboard_prize_payment_mode(leaderboard_category)

    total_paid = Decimal("0.00")
    top_kills = -1
    best_player_id = None

    winners_set = set()

    # ─── PROCESS MANUAL PRIZES (NEW) ──────────────────────────────────
    if data.manual_prizes:
        for entry in data.manual_prizes:
            user_id = entry.user_id
            amount = to_money(entry.amount)

            # 1. ALWAYS UPDATE PARTICIPANT STATS (kills and rank)
            participant = db.query(TournamentParticipant).filter(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.user_id == user_id
            ).first()
            if participant:
                participant.prize_amount = str(amount)
                participant.kills = entry.kills or 0
                participant.participant_rank = entry.rank
                db.add(participant)

            member_user = db.query(User).filter(User.id == user_id).with_for_update().first()
            if not member_user: continue

            if amount > 0:
                credit_wallet(member_user, amount, WALLET_BUCKET_WINNING)
                
            tx = WalletTransaction(
                user_id=member_user.id,
                amount=amount,
                transaction_type="PRIZE_WIN",
                status="SUCCESS",
                reference_id=f"MNL-{tournament_id}-{uuid.uuid4().hex[:6].upper()}",
                payment_mode=payout_payment_mode,
                remark=tournament.title
            )
            db.add(tx)
            total_paid += amount
            winners_set.add(user_id)
            
            if amount > 0:
                try:
                    add_user_notification(
                        db, member_user.id,
                        "TOURNAMENT WINNINGS! 🏆",
                        f"Congratulations! You've been awarded ₹{amount:.2f} for '{tournament.title}'. Check your wallet!",
                        "APP"
                    )
                except Exception: pass
    else:
        # FALLBACK: ORIGINAL KILL-BASED LOGIC
        for entry in data.kill_rewards:
            user_id = entry.user_id
            kills = entry.kills or 0
                
            participant = db.query(TournamentParticipant).filter(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.user_id == user_id
            ).first()
            if not participant: continue

            # Save kills to participant record (ALWAYS)
            participant.kills = kills
            db.add(participant)

            member_user = db.query(User).filter(User.id == user_id).with_for_update().first()
            if not member_user: continue

            member_prize = per_kill_prize * kills
            if member_prize > 0:
                credit_wallet(member_user, member_prize, WALLET_BUCKET_WINNING)
                
            tx = WalletTransaction(
                user_id=member_user.id,
                amount=member_prize,
                transaction_type="PRIZE_WIN",
                status="SUCCESS",
                reference_id=f"PRZ-{tournament_id}-{uuid.uuid4().hex[:6].upper()}",
                payment_mode=payout_payment_mode,
                remark=tournament.title
            )
            db.add(tx)
            total_paid += member_prize
            winners_set.add(user_id)
            if kills > top_kills:
                top_kills = kills
                best_player_id = user_id

            if member_prize > 0:
                try:
                    add_user_notification(
                        db, member_user.id,
                        "KILLS REWARD! 🔫",
                        f"You got {kills} kills in '{tournament.title}'! ₹{member_prize:.2f} credited to your wallet.",
                        "APP"
                    )
                except Exception: pass

    # SAFETY CHECK: Ensure total rewards don't exceed the intended prize pool.
    # We skip this for manual_prizes because the admin is explicitly overriding the pool (e.g. adding rank prizes).
    prize_pool = to_money(getattr(tournament, 'prize_pool', 0.0))
    if not data.manual_prizes and total_paid > prize_pool:
        # Roll back everything if the payout exceeds the pool (data entry error)
        raise HTTPException(
            status_code=400, 
            detail=f"Payout Error: Total rewards (₹{total_paid:.2f}) exceed the Prize Pool (₹{prize_pool:.2f}). Please verify kill counts."
        )

    if data.winner_id:
        tournament.winner_id = int(data.winner_id)
    elif best_player_id:
        tournament.winner_id = best_player_id
        
    tournament.status = "COMPLETED"
    db.add(tournament)
    db.commit()

    # Notify and record 0-reward transactions for non-winners
    try:
        all_parts = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id
        ).all()
        for p in all_parts:
            if p.user_id not in winners_set:
                # Create a zero-amount transaction so it shows up in "Match Lost" history
                lost_tx = WalletTransaction(
                    user_id=p.user_id,
                    amount=Decimal("0.00"),
                    transaction_type="PRIZE_WIN",
                    status="SUCCESS",
                    reference_id=f"LST-{tournament_id}-{uuid.uuid4().hex[:6].upper()}",
                    payment_mode=payout_payment_mode,
                    remark=tournament.title
                )
                db.add(lost_tx)
                
                add_user_notification(
                    db, p.user_id,
                    "Tournament Completed 🏆",
                    f"'{tournament.title}' has ended. Better luck next time!",
                    "APP"
                )
    except Exception as notify_err:
        logger.error(f"Failed to process non-winner records for tournament {tournament_id}: {notify_err}")

    logger.info(
        f"Tournament {tournament_id} concluded. "
        f"Total prize paid: ₹{total_paid} based on per_kill_prize ₹{per_kill_prize}"
    )
    return {"message": f"Tournament concluded. Paid a total of ₹{total_paid:.2f} based on kills."}


# ─────────────────────────────────────────────────────────────────
# Refund tournament — single definition (removed duplicate)
# ─────────────────────────────────────────────────────────────────

@router.post("/tournaments/{tournament_id}/refund")
def refund_tournament(
    tournament_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status in ["COMPLETED", "CANCELLED"]:
        raise HTTPException(status_code=400, detail="Cannot refund a completed or cancelled tournament")

    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).order_by(
        func.coalesce(TournamentParticipant.slot_no, 999999),
        TournamentParticipant.id.asc(),
    ).all()

    ref_count = 0
    for p in participants:
        user = db.query(User).filter(User.id == p.user_id).with_for_update().first()
        if not user:
            continue

        # SECURITY: Close the refund loophole.
        # Instead of refunding everything to DEPOSIT, we check how the user originally paid.
        # This information is stored in the JOIN_TOURNAMENT transaction's failure_reason field.
        join_tx = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.contains(f"TOUR:{tournament_id};")
        ).order_by(WalletTransaction.id.desc()).first()

        entry_fee = to_money(tournament.entry_fee)
        
        # Default fallback if no transaction record is found (should not happen normally)
        buckets_to_refund = {WALLET_BUCKET_DEPOSIT: entry_fee}
        
        if join_tx and join_tx.failure_reason:
            try:
                # Parse: TOUR:123;DEDUCT_BONUS:2.00;DEDUCT_DEPOSIT:8.00;DEDUCT_WINNING:0.00
                parts = join_tx.failure_reason.split(";")
                for part in parts:
                    if ":" in part:
                        key, val = part.split(":", 1)
                        if key == "DEDUCT_BONUS":
                            buckets_to_refund[WALLET_BUCKET_BONUS] = Decimal(val)
                        elif key == "DEDUCT_DEPOSIT":
                            buckets_to_refund[WALLET_BUCKET_DEPOSIT] = Decimal(val)
                        elif key == "DEDUCT_WINNING":
                            buckets_to_refund[WALLET_BUCKET_WINNING] = Decimal(val)
            except Exception as parse_err:
                logger.warning(f"Failed to parse refund distribution for user {user.id}: {parse_err}")

        # Execute bucket-specific credits
        for bucket, amount in buckets_to_refund.items():
            if amount > 0:
                credit_wallet(user, amount, bucket)

        ref_tx = WalletTransaction(
            user_id=user.id,
            amount=entry_fee,
            transaction_type="REFUND",
            status="SUCCESS",
            reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
            failure_reason=f"REFUND_TOUR:{tournament_id};ORIG_TX:{join_tx.id if join_tx else 'NONE'}"
        )
        db.add(ref_tx)
        db.add(user)
        ref_count += 1

    tournament.status = "CANCELLED"
    db.add(tournament)
    db.commit()

    logger.info(f"Tournament {tournament_id} cancelled. Refunded {ref_count} users.")
    return {"message": f"Refunded all {ref_count} participants"}


# ─────────────────────────────────────────────────────────────────
# Admin stats
# ─────────────────────────────────────────────────────────────────


def _get_today_finance_metrics(db: Session):
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    daily_recharged_today = float(db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.created_at >= today_start,
    ).scalar() or 0.0)

    daily_withdrawal_requested_today = float(db.query(func.sum(func.abs(WalletTransaction.amount))).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.created_at >= today_start,
    ).scalar() or 0.0)

    daily_withdrawal_success_today = float(db.query(func.sum(func.abs(WalletTransaction.amount))).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "SUCCESS",
        func.coalesce(WalletTransaction.updated_at, WalletTransaction.created_at) >= today_start,
    ).scalar() or 0.0)

    return {
        "daily_recharged_today": round(daily_recharged_today, 2),
        "daily_withdrawal_requested_today": round(daily_withdrawal_requested_today, 2),
        "daily_withdrawal_success_today": round(daily_withdrawal_success_today, 2),
    }

@router.get("/stats")
def get_admin_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    total_users       = db.query(User).count()
    total_tournaments = db.query(Tournament).count()

    # Base Metrics
    total_joins = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0

    total_revenue_pool = abs(float(total_joins))

    total_prizes = db.query(func.sum(Tournament.prize_pool)).filter(
        Tournament.status == "COMPLETED"
    ).scalar() or 0.0

    # Subtract refunds from revenue pool to get real estimated revenue
    total_refunds = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "REFUND",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0

    estimated_revenue = total_revenue_pool - float(total_prizes) - float(total_refunds)

    # NEW: Pending Withdrawals count
    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "PENDING"
    ).count()

    today_finance = _get_today_finance_metrics(db)

    # NEW: Daily Revenue for Chart (Last 7 Days)
    # We group by date of created_at
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    daily_res = db.query(
        func.date(WalletTransaction.created_at).label("day_date"),
        func.sum(func.abs(WalletTransaction.amount)).label("daily_sum")
    ).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.created_at >= seven_days_ago
    ).group_by("day_date").order_by("day_date").all()

    # Map to frontend format: [{ day: 'Mon', revenue: 4200 }, ...]
    # We'll fill missing days with 0 to keep the chart continuous
    now_utc = datetime.now(timezone.utc)
    days_map = { (now_utc - timedelta(days=i)).strftime("%Y-%m-%d"): 0.0 for i in range(7) }
    for r in daily_res:
        if r.day_date in days_map:
            days_map[r.day_date] = float(r.daily_sum)
    
    # Sort and format for Recharts
    chart_data = []
    # weekday names
    for date_str in sorted(days_map.keys()):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        chart_data.append({
            "day": dt.strftime("%a"), # 'Mon', 'Tue'...
            "revenue": days_map[date_str]
        })

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "total_revenue_pool": round(float(total_revenue_pool), 2),
        "total_prizes_distributed": round(float(total_prizes), 2),
        "estimated_platform_revenue": round(estimated_revenue, 2),
        "pending_withdrawals_count": pending_withdrawals,
        **today_finance,
        "daily_revenue": chart_data
    }


# ─────────────────────────────────────────────────────────────────
# Withdrawal management
# ─────────────────────────────────────────────────────────────────

def _refund_withdrawal_if_needed(
    db: Session,
    tx: WalletTransaction,
    admin_username: str,
    reason: str,
) -> Decimal:
    """Refund a pending withdrawal (and linked fee, if any) exactly once with immutable ledger entries."""
    if tx.transaction_type != "WITHDRAWAL":
        return Decimal("0.00")

    refund_reference = f"REFUND_WD_{tx.id}"
    existing_refund = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == refund_reference
    ).first()
    fee_refund_reference = f"REFUND_WDF_{tx.id}"
    existing_fee_refund = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == fee_refund_reference
    ).first()
    if existing_refund and existing_fee_refund:
        return Decimal("0.00")

    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for refund")

    total_refund_amount = Decimal("0.00")
    refund_markers: list[str] = []

    if not existing_refund:
        withdrawal_refund_amount = abs(Decimal(tx.amount or Decimal("0.00")))
        if withdrawal_refund_amount > Decimal("0.00"):
            # Withdrawal is debited from winning only, so refund goes back to winning.
            credit_wallet(user, withdrawal_refund_amount, WALLET_BUCKET_WINNING)
            refund_tx = WalletTransaction(
                user_id=tx.user_id,
                amount=withdrawal_refund_amount,
                transaction_type="WITHDRAWAL_REFUND",
                status="SUCCESS",
                reference_id=refund_reference,
                payment_mode="SYSTEM_REFUND",
                failure_reason=f"SOURCE_WITHDRAWAL:{tx.id};REASON:{reason};ADMIN:{admin_username}",
            )
            db.add(refund_tx)
            total_refund_amount += withdrawal_refund_amount
            refund_markers.append(f"WITHDRAW_REFUNDED:{refund_reference}")

    if not existing_fee_refund and tx.reference_id:
        fee_tx = (
            db.query(WalletTransaction)
            .filter(
                WalletTransaction.user_id == tx.user_id,
                WalletTransaction.transaction_type == "WITHDRAWAL_FEE",
                WalletTransaction.status == "SUCCESS",
                WalletTransaction.amount < Decimal("0.00"),
                WalletTransaction.failure_reason.contains(f"SOURCE_WITHDRAWAL_REF:{tx.reference_id}"),
            )
            .order_by(WalletTransaction.id.desc())
            .first()
        )

        if fee_tx:
            fee_refund_amount = abs(Decimal(fee_tx.amount or Decimal("0.00")))
            if fee_refund_amount > Decimal("0.00"):
                credit_wallet(user, fee_refund_amount, WALLET_BUCKET_WINNING)
                fee_refund_tx = WalletTransaction(
                    user_id=tx.user_id,
                    amount=fee_refund_amount,
                    transaction_type="WITHDRAWAL_FEE_REFUND",
                    status="SUCCESS",
                    reference_id=fee_refund_reference,
                    payment_mode="SYSTEM_REFUND",
                    failure_reason=(
                        f"SOURCE_WITHDRAWAL:{tx.id};SOURCE_WITHDRAWAL_FEE:{fee_tx.id};"
                        f"REASON:{reason};ADMIN:{admin_username}"
                    ),
                )
                db.add(fee_refund_tx)
                total_refund_amount += fee_refund_amount
                refund_markers.append(f"FEE_REFUNDED:{fee_refund_reference}")

    if refund_markers:
        tx.failure_reason = (
            f"REASON:{reason};ADMIN:{admin_username};"
            + ";".join(refund_markers)
        )

    db.add(user)
    return total_refund_amount


def process_withdrawal_approval(
    db: Session,
    tx: WalletTransaction,
    *,
    actor_label: str,
    source: str = "ADMIN_PANEL",
) -> None:
    tx.status = "SUCCESS"
    db.add(tx)
    db.commit()

    try:
        add_user_notification(
            db,
            tx.user_id,
            "Withdrawal Successful ✅",
            (
                f"Your withdrawal request of ₹{abs(float(tx.amount))} has been approved "
                "and sent to your UPI ID. Check your bank account."
            ),
            "WALLET",
        )
    except Exception:
        pass

    logger.info(
        "Withdrawal %s approved via %s by %s",
        tx.id,
        source,
        actor_label,
    )


def process_withdrawal_rejection(
    db: Session,
    tx: WalletTransaction,
    *,
    actor_label: str,
    reason_code: str,
    source: str = "ADMIN_PANEL",
) -> Decimal:
    tx.status = "FAILED"
    refunded = _refund_withdrawal_if_needed(
        db,
        tx,
        actor_label,
        reason_code,
    )

    db.add(tx)
    db.commit()

    try:
        add_user_notification(
            db,
            tx.user_id,
            "Withdrawal Rejected ❌",
            (
                f"Your withdrawal of ₹{abs(float(tx.amount))} has been rejected. "
                "The debited amount and any applicable withdrawal fee have been "
                "refunded to your winning wallet."
            ),
            "WALLET",
        )
    except Exception:
        pass

    logger.info(
        "Withdrawal %s rejected via %s by %s; refund=%0.2f",
        tx.id,
        source,
        actor_label,
        float(refunded),
    )
    return refunded

@router.get("/withdrawals")
def list_pending_withdrawals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    pending = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "PENDING"
    ).all()

    # FIXED: Bulk-load users to avoid N+1 queries
    user_ids = [tx.user_id for tx in pending]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    latest_upi_by_user: dict[int, str] = {}
    if user_ids:
        withdraw_accounts = (
            db.query(WithdrawUpiAccount)
            .filter(WithdrawUpiAccount.user_id.in_(user_ids))
            .order_by(WithdrawUpiAccount.id.desc())
            .all()
        )
        for account in withdraw_accounts:
            latest_upi_by_user.setdefault(account.user_id, account.upi_id)

    return [
        {
            "id":         tx.id,
            "user_id":    tx.user_id,
            "username":   users[tx.user_id].username if tx.user_id in users else "Unknown",
            "amount":     abs(float(tx.amount)),
            "created_at": tx.created_at,
            "upi_id":     tx.payu_txn_id or latest_upi_by_user.get(tx.user_id) or "N/A"
        }
        for tx in pending
    ]


@router.post("/withdrawals/{transaction_id}/approve")
def approve_withdrawal(
    transaction_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.id == transaction_id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.transaction_type != "WITHDRAWAL" or tx.status != "PENDING":
        raise HTTPException(status_code=400, detail="Invalid transaction or already processed")

    process_withdrawal_approval(
        db,
        tx,
        actor_label=current_user.username,
        source="ADMIN_PANEL",
    )
    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {"message": "Withdrawal approved"}


@router.post("/withdrawals/{transaction_id}/reject")
def reject_withdrawal(
    transaction_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.id == transaction_id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.transaction_type != "WITHDRAWAL" or tx.status != "PENDING":
        raise HTTPException(status_code=400, detail="Invalid transaction or already processed")

    process_withdrawal_rejection(
        db,
        tx,
        actor_label=current_user.username,
        reason_code="REJECTED_BY_ADMIN",
        source="ADMIN_PANEL",
    )
    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {"message": "Withdrawal rejected and refunded"}


# ─────────────────────────────────────────────────────────────────
# Tournament roster
# ─────────────────────────────────────────────────────────────────

@router.get("/tournaments/{tournament_id}/roster")
def get_tournament_roster(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).order_by(
        func.coalesce(TournamentParticipant.slot_no, 999999),
        TournamentParticipant.id.asc(),
    ).all()

    user_ids = [p.user_id for p in participants]
    # FIXED: Bulk fetch users
    user_map = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    def _serialize_participant(p: TournamentParticipant) -> dict:
        team_members = p.team_members
        primary = team_members[0] if team_members else None
        primary_level = (
            int(primary["level"]) if primary and primary.get("level") is not None else p.account_level
        )
        return {
            "id":            p.user_id,
            "user_id":       p.user_id,
            "username":      user_map[p.user_id].username     if p.user_id in user_map else "Unknown",
            "avatar_url":    user_map[p.user_id].profile_pic  if p.user_id in user_map else None,
            "game_username": primary["name"] if primary else p.game_username,
            "game_uid":      primary["uid"] if primary else p.game_uid,
            "account_level": primary_level,
            "team_members":  team_members,
            "slot_no":       p.slot_no,
            "slot_label":    f"S{p.slot_no}" if p.slot_no else None,
            "bgmi_id":       None,
            "freefire_id":   user_map[p.user_id].freefire_id  if p.user_id in user_map else None,
            "valorant_id":   None,
            "user": {
                "id":            p.user_id,
                "game_username": primary["name"] if primary else p.game_username,
                "full_name":     user_map[p.user_id].username if p.user_id in user_map else "Unknown",
                "phone":         user_map[p.user_id].phone_number if p.user_id in user_map else "",
                "username":      user_map[p.user_id].username if p.user_id in user_map else "Unknown"
            }
        }

    return [_serialize_participant(p) for p in participants]


@router.get("/tournaments/{tournament_id}/leaderboard")
def get_tournament_leaderboard(tournament_id: int, db: Session = Depends(get_db)):
    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).order_by(
        TournamentParticipant.participant_rank.asc().nulls_last(),
        TournamentParticipant.kills.desc()
    ).all()
    
    return [
        {
            "user_id": p.user_id,
            "username": p.user.username if p.user else "Unknown",
            "profile_pic": p.user.profile_pic if p.user else None,
            "rank": p.participant_rank,
            "kills": p.kills,
            "prize_amount": p.prize_amount,
            "slot_no": p.slot_no,
            "game_uid": p.game_uid,
            "game_username": p.game_username
        }
        for p in participants
    ]



@router.get("/tournaments/{tournament_id}/slots", response_model=TournamentSlotsBoardResponse)
def get_tournament_slots_admin(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    from api.tournaments import _build_slots_board

    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
    ).all()
    return _build_slots_board(tournament, participants)


# ─────────────────────────────────────────────────────────────────
# Banner management
# ─────────────────────────────────────────────────────────────────


def _normalize_banner_title(value: str) -> str:
    title = " ".join(value.strip().split())
    if len(title) < 2:
        raise HTTPException(status_code=400, detail="Banner title must be at least 2 characters")
    return title[:120]


def _normalize_banner_url(value: str, field_name: str) -> str:
    url = value.strip()
    if not url:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return url[:500]


def _normalize_optional_banner_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:500] if normalized else None


def _normalize_optional_banner_notes(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:300] if normalized else None


def _coerce_banner_active(status: str | None) -> bool:
    if status is None:
        return True
    normalized = status.strip().upper()
    if normalized in {"ACTIVE", "ENABLED"}:
        return True
    if normalized in {"INACTIVE", "DISABLED"}:
        return False
    raise HTTPException(status_code=400, detail="Invalid banner status. Use ACTIVE or INACTIVE")


def _validate_banner_schedule(starts_at: datetime | None, ends_at: datetime | None) -> None:
    if starts_at and ends_at and ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")


BANNER_STORAGE_DIR = "static/banners"
BANNER_TARGET_WIDTH = 1200
BANNER_TARGET_HEIGHT = 400

def _cleanup_banner_file(image_url: str):
    """If the image is stored locally in static/banners, delete it."""
    if not image_url:
        return
    
    # Check if URL belongs to our local static banners
    if "/static/banners/" in image_url:
        try:
            filename = image_url.rsplit("/", 1)[-1]
            file_path = os.path.join(BANNER_STORAGE_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info(f"Deleted orphan banner file: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup banner file {image_url}: {e}")

@router.post("/banners/upload")
async def upload_banner_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_admin),
):
    """Upload a banner image, resize to 1200x400, compress, and return the URL."""
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid file type. Only images are allowed.")

    data = await file.read()
    
    try:
        img = Image.open(io.BytesIO(data))
        
        # Enforce 3:1 aspect ratio by cropping or just resizing (Fit)
        # We'll use a high-quality resize to 1200x400
        img = img.convert("RGB")
        img = img.resize((BANNER_TARGET_WIDTH, BANNER_TARGET_HEIGHT), Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        compressed_data = output.getvalue()
        
        filename = f"banner_{uuid.uuid4().hex[:12]}.jpg"
        
        from services.storage import upload_file
        try:
            public_url = upload_file(compressed_data, filename, sub_dir="banners")
        except Exception as e:
            logger.error(f"Failed to upload banner to storage: {e}")
            # Fallback
            os.makedirs(BANNER_STORAGE_DIR, exist_ok=True)
            save_path = os.path.join(BANNER_STORAGE_DIR, filename)
            with open(save_path, "wb") as f:
                f.write(compressed_data)
            base_url = (settings.APP_URL or "").rstrip("/")
            public_url = f"{base_url}/static/banners/{filename}"

        return {"image_url": public_url}
        
    except Exception as e:
        logger.error(f"Banner upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to process image")

@router.post("/tournaments/upload")
async def upload_tournament_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_admin),
):
    """Upload a match/tournament icon, resize to 512x512, compress, and return the URL."""
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid file type. Only images are allowed.")

    data = await file.read()
    
    try:
        img = Image.open(io.BytesIO(data))
        
        # Enforce 1:1 aspect ratio for icons
        img = img.convert("RGB")
        img = img.resize((512, 512), Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        compressed_data = output.getvalue()
        
        filename = f"match_{uuid.uuid4().hex[:12]}.jpg"
        
        from services.storage import upload_file
        try:
            public_url = upload_file(compressed_data, filename, sub_dir="tournaments")
        except Exception as e:
            logger.error(f"Storage service upload failed: {e}")
            raise HTTPException(status_code=500, detail="Cloud storage upload failed.")

        return {"image_url": public_url}
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid image file or processing error.")

def _banner_status(banner: HomeBanner) -> str:
    if not banner.is_active:
        return "INACTIVE"

    now = datetime.utcnow()

    if banner.starts_at:
        starts_at = banner.starts_at
        if getattr(starts_at, "tzinfo", None) is not None:
            starts_at = starts_at.replace(tzinfo=None)
        if starts_at > now:
            return "SCHEDULED"

    if banner.ends_at:
        ends_at = banner.ends_at
        if getattr(ends_at, "tzinfo", None) is not None:
            ends_at = ends_at.replace(tzinfo=None)
        if ends_at <= now:
            return "EXPIRED"

    return "ACTIVE"


def _serialize_banner(banner: HomeBanner) -> dict:
    return {
        "id": banner.id,
        "title": banner.title,
        "image_url": banner.image_url,
        "redirect_url": banner.redirect_url,
        "notes": banner.notes,
        "page_key": banner.page_key,
        "sort_order": int(banner.sort_order or 0),
        "status": _banner_status(banner),
        "is_active": bool(banner.is_active),
        "starts_at": banner.starts_at,
        "ends_at": banner.ends_at,
        "created_at": banner.created_at,
        "updated_at": banner.updated_at,
    }


@router.get("/banners")
def list_banners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    banners = (
        db.query(HomeBanner)
        .order_by(HomeBanner.sort_order.asc(), HomeBanner.created_at.desc())
        .all()
    )
    return [_serialize_banner(banner) for banner in banners]


@router.post("/banners")
def create_banner(
    payload: BannerCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    title = _normalize_banner_title(payload.title)
    image_url = _normalize_banner_url(payload.image_url, "image_url")
    redirect_url = _normalize_optional_banner_url(payload.redirect_url)
    notes = _normalize_optional_banner_notes(payload.notes)
    _validate_banner_schedule(payload.starts_at, payload.ends_at)

    banner = HomeBanner(
        title=title,
        image_url=image_url,
        redirect_url=redirect_url,
        page_key=payload.page_key or "HOME",
        notes=notes,
        sort_order=int(payload.sort_order or 0),
        is_active=_coerce_banner_active(payload.status),
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
    )

    db.add(banner)
    db.commit()
    db.refresh(banner)
    logger.info("Banner created by admin=%s banner_id=%s", current_user.username, banner.id)
    return _serialize_banner(banner)


@router.put("/banners/{banner_id}")
def update_banner(
    banner_id: int,
    payload: BannerUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    banner = db.query(HomeBanner).filter(HomeBanner.id == banner_id).first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner not found")

    if payload.title is not None:
        banner.title = _normalize_banner_title(payload.title)

    if payload.image_url is not None:
        old_url = banner.image_url
        new_url = _normalize_banner_url(payload.image_url, "image_url")
        if old_url != new_url:
            _cleanup_banner_file(old_url)
        banner.image_url = new_url

    if payload.redirect_url is not None:
        banner.redirect_url = _normalize_optional_banner_url(payload.redirect_url)

    if payload.notes is not None:
        banner.notes = _normalize_optional_banner_notes(payload.notes)

    if payload.sort_order is not None:
        banner.sort_order = int(payload.sort_order)

    if payload.status is not None:
        banner.is_active = _coerce_banner_active(payload.status)

    if payload.page_key is not None:
        banner.page_key = payload.page_key

    next_starts_at = banner.starts_at
    next_ends_at = banner.ends_at
    if payload.starts_at is not None:
        next_starts_at = payload.starts_at
    if payload.ends_at is not None:
        next_ends_at = payload.ends_at

    _validate_banner_schedule(next_starts_at, next_ends_at)

    if payload.starts_at is not None:
        banner.starts_at = payload.starts_at
    if payload.ends_at is not None:
        banner.ends_at = payload.ends_at

    db.add(banner)
    db.commit()
    db.refresh(banner)
    logger.info("Banner updated by admin=%s banner_id=%s", current_user.username, banner_id)
    return _serialize_banner(banner)


@router.delete("/banners/{banner_id}")
def delete_banner(
    banner_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    banner = db.query(HomeBanner).filter(HomeBanner.id == banner_id).first()
    if not banner:
        raise HTTPException(status_code=404, detail="Banner not found")

    banner_title = banner.title
    image_url = banner.image_url
    
    db.delete(banner)
    db.commit()
    
    # Clean up file after successful DB deletion
    _cleanup_banner_file(image_url)
    
    logger.warning("Banner deleted by admin=%s banner_id=%s", current_user.username, banner_id)
    return {"message": f"Banner '{banner_title}' deleted"}


# ─────────────────────────────────────────────────────────────────
# Promo management
# ─────────────────────────────────────────────────────────────────

def _normalize_promo_code(code: str) -> str:
    cleaned = " ".join(code.strip().upper().split())
    normalized = cleaned.replace(" ", "_")
    if len(normalized) < 3:
        raise HTTPException(status_code=400, detail="Promo code must be at least 3 characters")
    return normalized[:40]


def _resolve_promo_reward_amount(
    reward_amount: float,
) -> Decimal:
    raw_amount = str(reward_amount)

    amount = Decimal(raw_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Promo reward amount must be greater than 0")

    return amount


def _coerce_promo_active(status: str | None) -> bool:
    if status is None:
        return True
    normalized = status.strip().upper()
    if normalized in {"ACTIVE", "ENABLED"}:
        return True
    if normalized in {"INACTIVE", "DISABLED", "EXPIRED"}:
        return False
    raise HTTPException(status_code=400, detail="Invalid promo status. Use ACTIVE or INACTIVE")


def _promo_status(promo: PromoCode) -> str:
    if not promo.is_active:
        return "INACTIVE"
    if (promo.uses_count or 0) >= (promo.max_uses or 0):
        return "EXHAUSTED"

    if promo.expires_at:
        expires_at = promo.expires_at
        if getattr(expires_at, "tzinfo", None) is not None:
            expires_at = expires_at.replace(tzinfo=None)
        if expires_at <= datetime.utcnow():
            return "EXPIRED"

    return "ACTIVE"


def _serialize_promo(promo: PromoCode) -> dict:
    reward_amount = float(promo.discount_amount or 0)
    return {
        "id": promo.id,
        "code": promo.code,
        "reward_amount": reward_amount,
        "discount": reward_amount,
        "uses": int(promo.uses_count or 0),
        "max_uses": int(promo.max_uses or 0),
        "status": _promo_status(promo),
        "is_active": bool(promo.is_active),
        "notes": promo.notes,
        "expires_at": promo.expires_at,
        "created_at": promo.created_at,
        "updated_at": promo.updated_at,
    }


def _promo_reward_reference(promo_id: int, user_id: int) -> str:
    return f"PROMO_{promo_id}_{user_id}"


def _promo_reversal_reference(reward_tx_id: int) -> str:
    return f"PROMO_REVOKE_{reward_tx_id}"


@router.get("/promos")
def list_promos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    promos = db.query(PromoCode).order_by(PromoCode.created_at.desc()).all()
    return [_serialize_promo(promo) for promo in promos]


@router.get("/promos/{promo_id}/usages")
def list_promo_usages(
    promo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="Promo not found")

    promo_marker = f"PROMO:{promo.code}"
    reward_transactions = (
        db.query(WalletTransaction)
        .filter(
            WalletTransaction.transaction_type == "PROMO_REWARD",
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.contains(promo_marker),
        )
        .order_by(WalletTransaction.created_at.desc())
        .all()
    )

    user_ids = list({tx.user_id for tx in reward_transactions})
    users_by_id = {
        user.id: user
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    reversal_references = [_promo_reversal_reference(tx.id) for tx in reward_transactions]
    reversed_reference_set = set()
    if reversal_references:
        reversed_reference_set = {
            row[0]
            for row in db.query(WalletTransaction.reference_id)
            .filter(
                WalletTransaction.reference_id.in_(reversal_references),
                WalletTransaction.status == "SUCCESS",
            )
            .all()
        }

    usages = []
    for tx in reward_transactions:
        user = users_by_id.get(tx.user_id)
        reversal_reference = _promo_reversal_reference(tx.id)
        amount = abs(Decimal(str(tx.amount or Decimal("0.00")))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        usages.append(
            {
                "transaction_id": tx.id,
                "reference_id": tx.reference_id,
                "user_id": tx.user_id,
                "username": user.username if user else "Unknown",
                "email": user.email if user else None,
                "wallet_balance": float(user.wallet_balance or 0) if user else 0,
                "amount": float(amount),
                "redeemed_at": tx.created_at,
                "is_reversed": reversal_reference in reversed_reference_set,
                "reversal_reference": reversal_reference,
            }
        )

    return {
        "promo": _serialize_promo(promo),
        "usages": usages,
    }


@router.post("/promos/{promo_id}/usages/{transaction_id}/revoke")
def revoke_promo_usage(
    promo_id: int,
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).with_for_update().first()
    if not promo:
        raise HTTPException(status_code=404, detail="Promo not found")

    tx = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Promo usage transaction not found")

    promo_marker = f"PROMO:{promo.code}"
    if (
        tx.transaction_type != "PROMO_REWARD"
        or tx.status != "SUCCESS"
        or promo_marker not in (tx.failure_reason or "")
    ):
        raise HTTPException(status_code=400, detail="Selected transaction does not belong to this promo usage")

    reversal_reference = _promo_reversal_reference(tx.id)
    existing_reversal = db.query(WalletTransaction.id).filter(
        WalletTransaction.reference_id == reversal_reference,
        WalletTransaction.status == "SUCCESS",
    ).first()
    if existing_reversal:
        raise HTTPException(status_code=409, detail="Promo money already taken back for this user")

    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    reversal_amount = abs(Decimal(str(tx.amount or Decimal("0.00")))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if reversal_amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Promo transaction has invalid reward amount")

    current_balance = get_total_balance(user)
    if current_balance < reversal_amount:
        raise HTTPException(
            status_code=400,
            detail=f"User has insufficient wallet balance. Required Rs {reversal_amount:.2f}, available Rs {current_balance:.2f}",
        )

    try:
        debit_wallet(
            user,
            reversal_amount,
            spend_order=(WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING, WALLET_BUCKET_BONUS),
        )
    except InsufficientWalletBalanceError:
        raise HTTPException(
            status_code=400,
            detail=f"User has insufficient wallet balance. Required Rs {reversal_amount:.2f}, available Rs {current_balance:.2f}",
        )
    if int(promo.uses_count or 0) > 0:
        promo.uses_count = int(promo.uses_count or 0) - 1

    reversal_tx = WalletTransaction(
        user_id=user.id,
        amount=-reversal_amount,
        transaction_type="PROMO_REVOKE",
        status="SUCCESS",
        reference_id=reversal_reference,
        payment_mode="ADMIN_REVERSAL",
        failure_reason=f"SOURCE_PROMO_TX:{tx.id};PROMO:{promo.code};ADMIN:{current_user.username}",
    )

    tx.failure_reason = f"{tx.failure_reason + '|' if tx.failure_reason else ''}REVERSED:{reversal_reference}"

    db.add(user)
    db.add(promo)
    db.add(tx)
    db.add(reversal_tx)
    db.commit()
    db.refresh(user)

    try:
        add_user_notification(
            db,
            user.id,
            "Promo Reward Reversed",
            f"Promo {promo.code} reward of Rs {reversal_amount:.2f} has been reversed by admin and deducted from your wallet.",
            "WALLET",
        )
    except Exception:
        pass

    logger.warning(
        "Promo reward reversed by admin=%s promo_id=%s tx_id=%s user_id=%s amount=%.2f",
        current_user.username,
        promo_id,
        tx.id,
        user.id,
        float(reversal_amount),
    )

    return {
        "message": "Promo money taken back from user wallet",
        "promo_id": promo_id,
        "user_id": user.id,
        "reversal_reference": reversal_reference,
        "deducted_amount": float(reversal_amount),
        "wallet_balance": float(get_total_balance(user)),
    }


@router.post("/promos")
def create_promo(
    payload: PromoCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    code = _normalize_promo_code(payload.code)
    exists = db.query(PromoCode).filter(PromoCode.code == code).first()
    if exists:
        raise HTTPException(status_code=400, detail="Promo code already exists")

    reward_amount = _resolve_promo_reward_amount(
        reward_amount=payload.reward_amount,
    )

    promo = PromoCode(
        code=code,
        discount_amount=reward_amount,
        max_uses=int(payload.max_uses),
        is_active=_coerce_promo_active(payload.status),
        notes=payload.notes.strip() if payload.notes else None,
        expires_at=payload.expires_at,
    )

    db.add(promo)
    db.commit()
    db.refresh(promo)
    logger.info("Promo created by admin=%s code=%s", current_user.username, promo.code)
    return _serialize_promo(promo)


@router.put("/promos/{promo_id}")
def update_promo(
    promo_id: int,
    payload: PromoUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="Promo not found")

    if payload.code is not None:
        normalized_code = _normalize_promo_code(payload.code)
        conflict = db.query(PromoCode).filter(PromoCode.code == normalized_code, PromoCode.id != promo_id).first()
        if conflict:
            raise HTTPException(status_code=400, detail="Promo code already exists")
        promo.code = normalized_code

    if payload.reward_amount is not None or payload.discount is not None:
        next_reward = payload.reward_amount if payload.reward_amount is not None else payload.discount
        promo.discount_amount = Decimal(str(next_reward)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if payload.max_uses is not None:
        if payload.max_uses < int(promo.uses_count or 0):
            raise HTTPException(status_code=400, detail="max_uses cannot be less than current uses")
        promo.max_uses = int(payload.max_uses)

    if payload.status is not None:
        promo.is_active = _coerce_promo_active(payload.status)

    if payload.notes is not None:
        promo.notes = payload.notes.strip() or None

    if payload.expires_at is not None:
        promo.expires_at = payload.expires_at

    db.add(promo)
    db.commit()
    db.refresh(promo)
    logger.info("Promo updated by admin=%s promo_id=%s", current_user.username, promo_id)
    return _serialize_promo(promo)


@router.delete("/promos/{promo_id}")
def delete_promo(
    promo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    promo = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="Promo not found")

    code = promo.code
    db.delete(promo)
    db.commit()
    logger.warning("Promo deleted by admin=%s code=%s", current_user.username, code)
    return {"message": f"Promo '{code}' deleted"}


# ─────────────────────────────────────────────────────────────────
# User management
# ─────────────────────────────────────────────────────────────────


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _compute_last_wallet_activity_for_user_ids(db: Session, user_ids: List[int]) -> dict[int, datetime]:
    if not user_ids:
        return {}

    rows = (
        db.query(
            WalletTransaction.user_id,
            func.max(WalletTransaction.created_at).label("last_activity_at"),
        )
        .filter(
            WalletTransaction.user_id.in_(user_ids),
            WalletTransaction.status == "SUCCESS",
        )
        .group_by(WalletTransaction.user_id)
        .all()
    )

    return {
        int(row.user_id): row.last_activity_at
        for row in rows
        if getattr(row, "last_activity_at", None)
    }


def _compute_admin_referral_stats(db: Session, user: User) -> dict:
    total_referrals = int(
        db.query(func.count(User.id))
        .filter(User.referred_by_id == user.id)
        .scalar()
        or 0
    )

    activated_referrals = int(
        db.query(func.count(func.distinct(User.id)))
        .join(WalletTransaction, WalletTransaction.user_id == User.id)
        .filter(
            User.referred_by_id == user.id,
            WalletTransaction.transaction_type == "ADD_MONEY",
            WalletTransaction.status == "SUCCESS",
        )
        .scalar()
        or 0
    )

    reward_summary = (
        db.query(
            func.coalesce(func.sum(WalletTransaction.amount), 0).label("total_earned"),
            func.max(WalletTransaction.created_at).label("last_reward_at"),
        )
        .filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.transaction_type.in_((
                REFERRAL_REWARD_TX_TYPE,
                "REFERRAL_MISSION_REWARD",
            )),
            WalletTransaction.status == "SUCCESS",
        )
        .first()
    )
    total_earned = float(Decimal(str(getattr(reward_summary, "total_earned", 0) or 0)))
    last_reward_at = getattr(reward_summary, "last_reward_at", None)

    referred_by_row = None
    if user.referred_by_id:
        referred_by_row = (
            db.query(User.id, User.username)
            .filter(User.id == user.referred_by_id)
            .first()
        )

    recent_referral_rows = (
        db.query(User.id, User.username, User.created_at)
        .filter(User.referred_by_id == user.id)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(10)
        .all()
    )

    recent_referral_ids = [int(row.id) for row in recent_referral_rows]
    recent_activated_ids: set[int] = set()
    if recent_referral_ids:
        activated_rows = (
            db.query(WalletTransaction.user_id)
            .filter(
                WalletTransaction.user_id.in_(recent_referral_ids),
                WalletTransaction.transaction_type == "ADD_MONEY",
                WalletTransaction.status == "SUCCESS",
            )
            .distinct()
            .all()
        )
        recent_activated_ids = {int(uid) for (uid,) in activated_rows}

    pending_referrals = max(total_referrals - activated_referrals, 0)
    activation_rate_pct = round((activated_referrals / total_referrals) * 100, 2) if total_referrals > 0 else 0.0

    return {
        "referral_code": user.referral_code,
        "referred_by": {
            "id": int(referred_by_row.id),
            "username": referred_by_row.username,
        } if referred_by_row else None,
        "total_referrals": total_referrals,
        "activated_referrals": activated_referrals,
        "pending_referrals": pending_referrals,
        "activation_rate_pct": activation_rate_pct,
        "total_earned": total_earned,
        "last_reward_at": last_reward_at,
        "recent_referrals": [
            {
                "user_id": int(row.id),
                "username": row.username,
                "joined_at": row.created_at,
                "has_first_deposit": int(row.id) in recent_activated_ids,
            }
            for row in recent_referral_rows
        ],
    }


def _serialize_admin_user(
    user: User,
    match_stats: dict | None = None,
    wallet_last_activity_at: datetime | None = None,
) -> dict:
    bucket_total = (user.deposit_balance or 0) + (user.winning_balance or 0) + (user.bonus_balance or 0)
    total_wallet_balance = float(bucket_total or 0)
    if total_wallet_balance <= 0:
        total_wallet_balance = float(user.wallet_balance or 0)

    last_wallet_activity = (
        _to_naive_utc(wallet_last_activity_at)
        or _to_naive_utc(user.created_at)
    )
    wallet_is_inactive_7d = True
    wallet_inactive_since_days = None
    if last_wallet_activity:
        inactivity_delta = datetime.utcnow() - last_wallet_activity
        wallet_inactive_since_days = max(int(inactivity_delta.total_seconds() // 86400), 0)
        wallet_is_inactive_7d = inactivity_delta >= timedelta(days=7)

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "phone_number": user.phone_number,
        "role": user.role,
        "wallet_balance": float(user.wallet_balance or 0),
        "deposit_balance": float(user.deposit_balance or 0),
        "winning_balance": float(user.winning_balance or 0),
        "bonus_balance": float(user.bonus_balance or 0),
        "total_wallet_balance": total_wallet_balance,
        "upi_id": None,
        "upi_account_holder_name": None,
        "profile_pic": user.profile_pic,
        "bio": user.bio,
        "bgmi_id": None,
        "valorant_id": None,
        "freefire_id": user.freefire_id,
        "is_active": user.is_active,
        "referral_code": user.referral_code,
        "referred_by_id": user.referred_by_id,
        "token_version": user.token_version,
        "last_login_ip": user.last_login_ip,
        "last_login_device": user.last_login_device,
        "last_login_at": user.last_login_at,
        "wallet_last_activity_at": last_wallet_activity,
        "wallet_is_inactive_7d": wallet_is_inactive_7d,
        "wallet_inactive_since_days": wallet_inactive_since_days,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "match_stats": match_stats or empty_user_match_stats(),
    }

@router.get("/users")
def search_users(
    query: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    search_text = (query or "").strip()
    filters = []
    if search_text:
        filters.append(User.username.ilike(f"%{search_text}%"))
        filters.append(User.email.ilike(f"%{search_text}%"))
        filters.append(User.phone_number.ilike(f"%{search_text}%"))
        if search_text.isdigit():
            filters.append(User.id == int(search_text))

    if filters:
        users = (
            db.query(User)
            .filter(or_(*filters))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(50)
            .all()
        )
    else:
        users = (
            db.query(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(50)
            .all()
        )

    user_ids = [user.id for user in users]
    stats_map = compute_match_stats_for_user_ids(db, user_ids)
    wallet_activity_map = _compute_last_wallet_activity_for_user_ids(db, user_ids)
    return [
        _serialize_admin_user(user, stats_map.get(user.id), wallet_activity_map.get(user.id))
        for user in users
    ]


@router.get("/users/{user_id}")
def get_user_detail(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    wallet_activity_map = _compute_last_wallet_activity_for_user_ids(db, [user_id])
    payload = _serialize_admin_user(
        user,
        compute_match_stats_for_user(db, user_id),
        wallet_activity_map.get(user_id),
    )
    payload["referral_stats"] = _compute_admin_referral_stats(db, user)
    latest_upi = (
        db.query(WithdrawUpiAccount)
        .filter(WithdrawUpiAccount.user_id == user_id)
        .order_by(WithdrawUpiAccount.created_at.desc(), WithdrawUpiAccount.id.desc())
        .first()
    )
    if latest_upi:
        payload["upi_id"] = latest_upi.upi_id
        payload["upi_account_holder_name"] = latest_upi.account_holder_name
    return payload


@router.get("/users/{user_id}/wallet-transactions", response_model=List[AdminWalletTransactionResponse])
def get_user_wallet_transactions(
    user_id: int,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    _ = current_user

    user_exists = db.query(User.id).filter(User.id == user_id).first()
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")

    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)

    rows = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )

    return [
        {
            "id": tx.id,
            "amount": float(tx.amount or 0),
            "transaction_type": tx.transaction_type,
            "status": tx.status,
            "reference_id": tx.reference_id,
            "payment_mode": tx.payment_mode,
            "failure_reason": tx.failure_reason,
            "created_at": tx.created_at,
        }
        for tx in rows
    ]


@router.get("/users/{user_id}/stats")
def get_user_stats(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return compute_match_stats_for_user(db, user_id)


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    status: UserStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = status.is_active

    # SECURITY: Increment token_version to instantly revoke all existing JWTs
    # for this user — they cannot use their old token even if it hasn't expired.
    if not status.is_active:
        current_tv = getattr(user, "token_version", 0) or 0
        user.token_version = current_tv + 1
        logger.info(f"Revoked all tokens for user {user_id} (token_version -> {user.token_version})")
    else:
        clear_otp_lock_for_user_sync(
            db,
            user=user,
            admin_id=current_user.id,
            note="OTP limit reset from admin status update",
        )
        clear_activity_locks_for_user_sync(
            db,
            user=user,
            admin_id=current_user.id,
            note="Activity limits reset from admin status update",
        )

    db.add(user)
    db.commit()
    status_str = "Active" if status.is_active else "Banned"
    logger.info(f"User {user_id} set to {status_str} by admin={current_user.username}")
    return {"message": f"User {user.username} is now {status_str}"}


_RESTRICTION_PAGE_LABELS = {
    "HOME": "Home",
    "TOURNAMENTS": "Tournaments",
    "WALLET": "Wallet",
    "SPIN": "Spin",
    "REFERRAL": "Referral",
    "PROFILE": "Profile",
    "SUPPORT": "Support",
}


def _serialize_admin_restriction_entry(
    restriction: UserRestriction,
    users_by_id: dict[int, User],
    admins_by_id: dict[int, User],
) -> dict:
    payload = serialize_user_restriction(restriction)
    user = users_by_id.get(restriction.user_id)
    creator = admins_by_id.get(restriction.created_by_admin_id or 0)
    lifter = admins_by_id.get(restriction.lifted_by_admin_id or 0)

    payload.update({
        "user_id": restriction.user_id,
        "username": user.username if user else None,
        "email": user.email if user else None,
        "user_is_active": bool(user.is_active) if user else None,
        "is_active": bool(restriction.is_active),
        "is_currently_active": is_restriction_currently_active(restriction),
        "created_by_admin_id": restriction.created_by_admin_id,
        "created_by_admin": creator.username if creator else None,
        "lifted_by_admin_id": restriction.lifted_by_admin_id,
        "lifted_by_admin": lifter.username if lifter else None,
        "lifted_at": restriction.lifted_at,
        "lift_note": restriction.lift_note,
        "page_label": _RESTRICTION_PAGE_LABELS.get(payload.get("page_key") or "", payload.get("page_key")),
    })
    return payload


@router.get("/restrictions/page-keys")
def list_restriction_page_keys(
    current_user: User = Depends(get_current_active_admin),
):
    return [
        {"key": key, "label": _RESTRICTION_PAGE_LABELS.get(key, key)}
        for key in sorted(VALID_RESTRICTION_PAGE_KEYS)
    ]


@router.get("/restrictions")
def list_user_restrictions(
    query: str = "",
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    restrictions_query = db.query(UserRestriction)
    if not include_inactive:
        restrictions_query = restrictions_query.filter(UserRestriction.is_active == True)

    restrictions = restrictions_query.order_by(UserRestriction.created_at.desc()).all()
    if not include_inactive:
        now = utcnow_naive()
        restrictions = [r for r in restrictions if is_restriction_currently_active(r, now)]

    user_ids = {r.user_id for r in restrictions}
    admin_ids = {
        admin_id
        for r in restrictions
        for admin_id in (r.created_by_admin_id, r.lifted_by_admin_id)
        if admin_id
    }

    users_by_id = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}
    admins_by_id = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(admin_ids)).all()
    } if admin_ids else {}

    filtered = restrictions
    q = (query or "").strip().lower()
    if q:
        def _matches(r: UserRestriction) -> bool:
            user = users_by_id.get(r.user_id)
            if str(r.user_id) == q:
                return True
            if user and q in (user.username or "").lower():
                return True
            if user and q in (user.email or "").lower():
                return True
            return False

        filtered = [r for r in restrictions if _matches(r)]

    return [
        _serialize_admin_restriction_entry(r, users_by_id, admins_by_id)
        for r in filtered
    ]


@router.get("/users/{user_id}/restrictions")
def list_user_restrictions_for_user(
    user_id: int,
    include_inactive: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    q = db.query(UserRestriction).filter(UserRestriction.user_id == user_id)
    if not include_inactive:
        q = q.filter(UserRestriction.is_active == True)
    restrictions = q.order_by(UserRestriction.created_at.desc()).all()
    if not include_inactive:
        now = utcnow_naive()
        restrictions = [r for r in restrictions if is_restriction_currently_active(r, now)]

    admins_by_id = {
        admin.id: admin
        for admin in db.query(User).filter(
            User.id.in_({
                admin_id
                for r in restrictions
                for admin_id in (r.created_by_admin_id, r.lifted_by_admin_id)
                if admin_id
            })
        ).all()
    } if restrictions else {}

    users_by_id = {user.id: user}
    return [
        _serialize_admin_restriction_entry(r, users_by_id, admins_by_id)
        for r in restrictions
    ]


@router.post("/restrictions")
def create_user_restriction(
    payload: RestrictionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    user = db.query(User).filter(User.id == payload.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "ADMIN":
        raise HTTPException(status_code=400, detail="Admin accounts cannot be restricted from this endpoint")

    try:
        scope = normalize_restriction_scope(payload.scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        page_key = normalize_restriction_page_key(payload.page_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if scope == RESTRICTION_SCOPE_PAGE and not page_key:
        raise HTTPException(status_code=400, detail="page_key is required when scope=PAGE")
    if scope == RESTRICTION_SCOPE_FULL_APP:
        page_key = None

    starts_at = to_naive(payload.starts_at) or utcnow_naive()
    ends_at = to_naive(payload.ends_at)
    if ends_at and ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")

    if scope == RESTRICTION_SCOPE_FULL_APP:
        already_active = get_active_restrictions_for_user(
            db,
            user.id,
            scope=RESTRICTION_SCOPE_FULL_APP,
        )
    else:
        already_active = get_active_restrictions_for_user(
            db,
            user.id,
            scope=RESTRICTION_SCOPE_PAGE,
            page_key=page_key,
        )
    if already_active:
        raise HTTPException(status_code=409, detail="An active restriction already exists for this target")

    restriction = UserRestriction(
        user_id=user.id,
        scope=scope,
        page_key=page_key,
        reason=(payload.reason or "").strip() or None,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=True,
        created_by_admin_id=current_user.id,
    )

    db.add(restriction)
    db.commit()
    db.refresh(restriction)

    try:
        add_user_notification(
            db,
            user.id,
            "Access Restriction Applied",
            build_restriction_detail(restriction),
            "SYSTEM",
        )
    except Exception:
        pass

    logger.info(
        "Restriction created by admin=%s user_id=%s scope=%s page=%s ends_at=%s",
        current_user.username,
        user.id,
        scope,
        page_key,
        ends_at.isoformat() if ends_at else None,
    )

    return {
        "message": "Restriction added successfully",
        "restriction": _serialize_admin_restriction_entry(
            restriction,
            users_by_id={user.id: user},
            admins_by_id={current_user.id: current_user},
        ),
    }


@router.post("/restrictions/bulk")
def create_bulk_user_restrictions(
    payload: BulkRestrictionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    try:
        scope = normalize_restriction_scope(payload.scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        page_key = normalize_restriction_page_key(payload.page_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if scope == RESTRICTION_SCOPE_PAGE and not page_key:
        raise HTTPException(status_code=400, detail="page_key is required when scope=PAGE")
    if scope == RESTRICTION_SCOPE_FULL_APP:
        page_key = None

    starts_at = to_naive(payload.starts_at) or utcnow_naive()
    ends_at = to_naive(payload.ends_at)
    if ends_at and ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")

    target_users = (
        db.query(User.id)
        .filter(User.role == "USER")
        .order_by(User.id.asc())
        .all()
    )
    target_user_ids = [row[0] for row in target_users]
    if not target_user_ids:
        raise HTTPException(status_code=404, detail="No user accounts found")

    restriction_query = db.query(UserRestriction).filter(
        UserRestriction.user_id.in_(target_user_ids),
        UserRestriction.is_active == True,
        UserRestriction.scope == scope,
    )
    if scope == RESTRICTION_SCOPE_PAGE:
        restriction_query = restriction_query.filter(UserRestriction.page_key == page_key)

    existing_active_restrictions = restriction_query.all()
    existing_user_ids = {
        restriction.user_id
        for restriction in existing_active_restrictions
        if is_restriction_currently_active(restriction)
    }

    create_user_ids = [user_id for user_id in target_user_ids if user_id not in existing_user_ids]
    if not create_user_ids:
        raise HTTPException(
            status_code=409,
            detail="An active restriction already exists for every target user",
        )

    restrictions = [
        UserRestriction(
            user_id=user_id,
            scope=scope,
            page_key=page_key,
            reason=(payload.reason or "").strip() or None,
            starts_at=starts_at,
            ends_at=ends_at,
            is_active=True,
            created_by_admin_id=current_user.id,
        )
        for user_id in create_user_ids
    ]

    db.add_all(restrictions)
    db.commit()

    notification_detail = build_restriction_detail(restrictions[0])
    for user_id in create_user_ids:
        try:
            add_user_notification(
                db,
                user_id,
                "Access Restriction Applied",
                notification_detail,
                "SYSTEM",
            )
        except Exception:
            pass

    logger.info(
        "Bulk restriction created by admin=%s target_users=%s created=%s scope=%s page=%s ends_at=%s",
        current_user.username,
        len(target_user_ids),
        len(create_user_ids),
        scope,
        page_key,
        ends_at.isoformat() if ends_at else None,
    )

    return {
        "message": "Bulk restriction added successfully",
        "scope": scope,
        "page_key": page_key,
        "target_users": len(target_user_ids),
        "created_restrictions": len(create_user_ids),
        "skipped_existing": len(target_user_ids) - len(create_user_ids),
    }


@router.post("/restrictions/{restriction_id}/unlock")
def unlock_user_restriction(
    restriction_id: int,
    payload: RestrictionUnlockRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    restriction = db.query(UserRestriction).filter(
        UserRestriction.id == restriction_id
    ).with_for_update().first()
    if not restriction:
        raise HTTPException(status_code=404, detail="Restriction not found")

    if not restriction.is_active:
        return {"message": "Restriction already unlocked"}

    restriction.is_active = False
    restriction.lifted_by_admin_id = current_user.id
    restriction.lifted_at = utcnow_naive()
    restriction.lift_note = (payload.note or "").strip() or None

    user = db.query(User).filter(User.id == restriction.user_id).with_for_update().first()
    if user and not user.is_active:
        has_other_full_app_restriction = bool(
            get_active_restrictions_for_user(
                db,
                user.id,
                scope=RESTRICTION_SCOPE_FULL_APP,
            )
        )
        if not has_other_full_app_restriction:
            user.is_active = True
            db.add(user)

    if user and restriction.scope == RESTRICTION_SCOPE_FULL_APP:
        clear_otp_lock_for_user_sync(
            db,
            user=user,
            admin_id=current_user.id,
            note=(payload.note or "").strip() or "Restriction unlocked from admin panel",
        )

    db.add(restriction)
    db.commit()
    db.refresh(restriction)

    if user:
        try:
            add_user_notification(
                db,
                user.id,
                "Restriction Lifted",
                "An admin has removed your account restriction.",
                "SYSTEM",
            )
        except Exception:
            pass

    logger.info(
        "Restriction unlocked by admin=%s restriction_id=%s user_id=%s",
        current_user.username,
        restriction.id,
        restriction.user_id,
    )
    return {"message": "Restriction unlocked successfully"}


@router.post("/restrictions/unlock-all")
def unlock_all_user_restrictions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    active_restrictions = [
        restriction
        for restriction in db.query(UserRestriction)
        .filter(UserRestriction.is_active == True)
        .with_for_update()
        .all()
        if is_restriction_currently_active(restriction)
    ]
    if not active_restrictions:
        return {"message": "No active restrictions found"}

    now = utcnow_naive()
    unlocked_count = 0
    impacted_user_ids = {r.user_id for r in active_restrictions}

    for restriction in active_restrictions:
        restriction.is_active = False
        restriction.lifted_by_admin_id = current_user.id
        restriction.lifted_at = now
        restriction.lift_note = "Bulk unlock all from command deck"
        db.add(restriction)
        unlocked_count += 1

    # Correct reactivity: check if any impacted users can now be activated
    if impacted_user_ids:
        target_users = (
            db.query(User)
            .filter(User.id.in_(impacted_user_ids), User.is_active == False)
            .with_for_update()
            .all()
        )
        for user in target_users:
            # Check for any remaining active full-app restrictions
            # (Note: we already marked the ones we found as is_active=False in memory,
            # but they are still True in DB until commit. SQLAlchemy session reflects 
            # the memory state for queries if autoflush is on, but it's safer to check 
            # carefully if we are doing many updates.)
            has_other_full_app = db.query(UserRestriction).filter(
                UserRestriction.user_id == user.id,
                UserRestriction.is_active == True,
                UserRestriction.scope == RESTRICTION_SCOPE_FULL_APP
            ).first()
            
            if not has_other_full_app:
                user.is_active = True
                db.add(user)

    db.commit()
    logger.warning(
        "Bulk unlock-all executed by admin=%s: revoked=%s restrictions across users=%s",
        current_user.username,
        unlocked_count,
        len(impacted_user_ids),
    )
    return {
        "message": f"Successfully lifted {unlocked_count} restriction(s).",
        "unlocked_count": unlocked_count,
    }


@router.delete("/users/{user_id}")
def delete_user_account(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account")

    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "ADMIN":
        raise HTTPException(status_code=400, detail="Admin accounts cannot be deleted from this action")

    try:
        from sqlalchemy import text as _t

        deleted_username = user.username
        deleted_email    = user.email

        db.flush()  # push any pending ORM state before raw SQL
        uid = user_id

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 1 — SET NULL on every nullable FK column referencing this user
        # Must happen BEFORE any DELETE so FK constraints don't fire.
        # ══════════════════════════════════════════════════════════════════════

        # chat_sessions: admin/helper columns
        db.execute(_t("UPDATE chat_sessions SET attended_by_admin_id = NULL WHERE attended_by_admin_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_sessions SET blocked_by_admin_id  = NULL WHERE blocked_by_admin_id  = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_sessions SET ended_by_user_id     = NULL WHERE ended_by_user_id     = :uid"), {"uid": uid})

        # chat_messages: thread owner and sender (nullable, but FK constrained)
        db.execute(_t("UPDATE chat_messages SET thread_user_id = NULL WHERE thread_user_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_messages SET sender_id      = NULL WHERE sender_id      = :uid"), {"uid": uid})

        # user_restrictions: admin audit columns
        db.execute(_t("UPDATE user_restrictions SET created_by_admin_id = NULL WHERE created_by_admin_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE user_restrictions SET lifted_by_admin_id  = NULL WHERE lifted_by_admin_id  = :uid"), {"uid": uid})

        # user_activity_locks: admin unlock column
        db.execute(_t("UPDATE user_activity_locks SET unlocked_by_admin_id = NULL WHERE unlocked_by_admin_id = :uid"), {"uid": uid})

        # otp_phone_locks: both FK columns are nullable
        db.execute(_t("UPDATE otp_phone_locks SET user_id              = NULL WHERE user_id              = :uid"), {"uid": uid})
        db.execute(_t("UPDATE otp_phone_locks SET unlocked_by_admin_id = NULL WHERE unlocked_by_admin_id = :uid"), {"uid": uid})

        # users: self-referential referral
        db.execute(_t("UPDATE users SET referred_by_id = NULL WHERE referred_by_id = :uid"), {"uid": uid})
        referred_updates_r = db.execute(_t("SELECT COUNT(*) FROM users WHERE referred_by_id IS NULL AND id != :uid"), {"uid": uid})

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 2 — DELETE child rows where user_id is NOT NULL (must own them)
        # ══════════════════════════════════════════════════════════════════════

        # chat_messages that belong to this user's OWN sessions
        r = db.execute(_t("""
            DELETE FROM chat_messages
            WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_id = :uid)
        """), {"uid": uid})
        deleted_chat_messages = r.rowcount

        # chat_sessions owned by this user
        r = db.execute(_t("DELETE FROM chat_sessions WHERE user_id = :uid"), {"uid": uid})
        deleted_chat_sessions = r.rowcount

        # notifications
        r = db.execute(_t("DELETE FROM notifications WHERE user_id = :uid"), {"uid": uid})
        deleted_notifications = r.rowcount

        # tournament_participants
        r = db.execute(_t("DELETE FROM tournament_participants WHERE user_id = :uid"), {"uid": uid})
        deleted_participants = r.rowcount

        # wallet_transactions
        r = db.execute(_t("DELETE FROM wallet_transactions WHERE user_id = :uid"), {"uid": uid})
        deleted_transactions = r.rowcount

        # user_restrictions  (user_id NOT NULL)
        r = db.execute(_t("DELETE FROM user_restrictions WHERE user_id = :uid"), {"uid": uid})
        deleted_restrictions = r.rowcount

        # user_activity_locks  (user_id NOT NULL)
        r = db.execute(_t("DELETE FROM user_activity_locks WHERE user_id = :uid"), {"uid": uid})
        deleted_activity_locks = r.rowcount

        # withdraw_upi_accounts  (user_id NOT NULL)
        db.execute(_t("DELETE FROM withdraw_upi_accounts WHERE user_id = :uid"), {"uid": uid})

        # email_otp_logs  (user_id FK — table exists in DB, no ORM model)
        db.execute(_t("DELETE FROM email_otp_logs WHERE user_id = :uid"), {"uid": uid})
        
        # admin_access_sessions  (user_id NOT NULL)
        db.execute(_t("DELETE FROM admin_access_sessions WHERE user_id = :uid"), {"uid": uid})

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 3 — Delete the user itself (all constraints are cleared)
        # ══════════════════════════════════════════════════════════════════════
        profile_pic_url = user.profile_pic
        db.execute(_t("DELETE FROM users WHERE id = :uid"), {"uid": uid})
        db.commit()

        # PHASE 4 — Cleanup profile picture from disk
        if profile_pic_url and "/static/profile_pics/" in profile_pic_url:
            try:
                filename = profile_pic_url.rsplit("/", 1)[-1]
                path = os.path.join("static/profile_pics", filename)
                if os.path.isfile(path):
                    os.remove(path)
                    logger.info(f"Deleted profile pic for deleted user {user_id}: {path}")
            except Exception as e:
                logger.warning(f"Failed to delete profile pic file for user {user_id}: {e}")

        logger.warning(
            f"User deleted by admin={current_user.username}: user_id={user_id}, "
            f"username={deleted_username}, email={deleted_email} | "
            f"restrictions={deleted_restrictions}, activity_locks={deleted_activity_locks}, "
            f"transactions={deleted_transactions}, participants={deleted_participants}, "
            f"notifications={deleted_notifications}, chat_sessions={deleted_chat_sessions}, "
            f"chat_messages={deleted_chat_messages}"
        )

        return {
            "message": f"User #{user_id} ({deleted_username}) deleted successfully",
            "deleted_restrictions": deleted_restrictions,
            "deleted_activity_locks": deleted_activity_locks,
            "deleted_transactions": deleted_transactions,
            "deleted_participants": deleted_participants,
            "deleted_notifications": deleted_notifications,
            "deleted_chat_sessions": deleted_chat_sessions,
            "deleted_chat_messages": deleted_chat_messages,
        }


    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete user")


@router.post("/users/{user_id}/adjust-funds")
def adjust_user_funds(
    user_id: int,
    amount: float,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    # Cap single adjustment to prevent extreme accidental mass crediting.
    # Updated: Reduced to 100 Crore (100,000,000) to keep dashboard layout stable.
    if abs(amount) > 100_000_000:
        raise HTTPException(status_code=400, detail="Single adjustment limit exceeded (Safety Cap: 10 Crore)")

    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    decimal_amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if decimal_amount > Decimal("0.00"):
        credit_wallet(user, decimal_amount, WALLET_BUCKET_DEPOSIT)
    elif decimal_amount < Decimal("0.00"):
        try:
            debit_wallet(
                user,
                abs(decimal_amount),
                spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING),
            )
        except InsufficientWalletBalanceError:
            raise HTTPException(status_code=400, detail="Adjustment would result in negative balance")

    tx = WalletTransaction(
        user_id=user_id,
        amount=decimal_amount,
        transaction_type="ADMIN_ADJUSTMENT",
        status="SUCCESS",
        reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
        failure_reason=f"ADMIN:{current_user.username};REASON:{(reason or 'Manual wallet adjustment').strip()[:200]}"
    )
    db.add(tx)
    db.add(user)
    db.commit()

    logger.info(
        f"Admin adjustment: admin={current_user.username} user={user_id} "
        f"amount={decimal_amount} reason={reason[:100]}"
    )
    return {"message": f"Balance updated. New balance: ₹{float(get_total_balance(user)):.2f}"}


@router.put("/users/{user_id}/wallet-buckets")
def update_user_wallet_buckets(
    user_id: int,
    payload: UserWalletBucketsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Keep existing migration-safe behavior before direct bucket assignment.
    ensure_wallet_buckets(user)

    old_deposit = float(user.deposit_balance or 0)
    old_winning = float(user.winning_balance or 0)
    old_bonus = float(user.bonus_balance or 0)

    new_deposit_decimal = to_money(payload.deposit_balance)
    new_winning_decimal = to_money(payload.winning_balance)
    new_bonus_decimal = to_money(payload.bonus_balance)
    new_wallet_total_decimal = to_money(new_deposit_decimal + new_winning_decimal + new_bonus_decimal)

    if new_deposit_decimal > MAX_NUMERIC_12_2:
        raise HTTPException(
            status_code=422,
            detail=f"deposit_balance exceeds maximum allowed value ({MAX_NUMERIC_12_2:.2f})",
        )
    if new_winning_decimal > MAX_NUMERIC_12_2:
        raise HTTPException(
            status_code=422,
            detail=f"winning_balance exceeds maximum allowed value ({MAX_NUMERIC_12_2:.2f})",
        )
    if new_bonus_decimal > MAX_NUMERIC_12_2:
        raise HTTPException(
            status_code=422,
            detail=f"bonus_balance exceeds maximum allowed value ({MAX_NUMERIC_12_2:.2f})",
        )
    if new_wallet_total_decimal > MAX_NUMERIC_12_2:
        raise HTTPException(
            status_code=422,
            detail=(
                "Total wallet balance exceeds maximum allowed value "
                f"({MAX_NUMERIC_12_2:.2f})"
            ),
        )

    user.deposit_balance = new_deposit_decimal
    user.winning_balance = new_winning_decimal
    user.bonus_balance = new_bonus_decimal
    sync_wallet_total(user)

    new_deposit = float(user.deposit_balance or 0)
    new_winning = float(user.winning_balance or 0)
    new_bonus = float(user.bonus_balance or 0)

    reason = (payload.reason or "Manual wallet bucket update").strip()[:200]

    # Build per-bucket change summaries for user-facing display
    def _change_str(name: str, old: float, new: float) -> str:
        diff = new - old
        if diff > 0:
            return f"{name}: +₹{diff:.2f}"
        elif diff < 0:
            return f"{name}: -₹{abs(diff):.2f}"
        return f"{name}: no change"

    changes = []
    if new_deposit != old_deposit:
        changes.append(_change_str("Deposit", old_deposit, new_deposit))
    if new_winning != old_winning:
        changes.append(_change_str("Winning", old_winning, new_winning))
    if new_bonus != old_bonus:
        changes.append(_change_str("Bonus", old_bonus, new_bonus))

    # Net amount for the transaction record: positive = net credit, negative = net debit
    net_change = (new_deposit + new_winning + new_bonus) - (old_deposit + old_winning + old_bonus)
    total_amount = to_money(net_change)

    tx = WalletTransaction(
        user_id=user_id,
        amount=total_amount,
        transaction_type="ADMIN_BUCKET_SET",
        status="SUCCESS",
        reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
        failure_reason=(
            f"ADMIN:{current_user.username};"
            + ";".join(changes) + ";"
            f"REASON:{reason}"
        ),
    )

    db.add(user)
    db.add(tx)
    try:
        db.commit()
    except DataError:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Wallet bucket amount is out of allowed range. "
                f"Maximum storable value is {MAX_NUMERIC_12_2:.2f}."
            ),
        )

    logger.info(
        "Admin bucket update: admin=%s user=%s deposit=%.2f winning=%.2f bonus=%.2f reason=%s",
        current_user.username,
        user_id,
        float(user.deposit_balance or 0),
        float(user.winning_balance or 0),
        float(user.bonus_balance or 0),
        reason,
    )

    return {
        "message": "Wallet buckets updated",
        "deposit_balance": float(user.deposit_balance or 0),
        "winning_balance": float(user.winning_balance or 0),
        "bonus_balance": float(user.bonus_balance or 0),
        "wallet_balance": float(user.wallet_balance or 0),
    }


# ─────────────────────────────────────────────────────────────────
# System config
# ─────────────────────────────────────────────────────────────────

@router.get("/config")
def get_system_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    return db.query(SystemConfig).all()


@router.get("/developer/config")
def get_developer_system_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    return get_system_configs(db=db, current_user=current_user)


@router.put("/config")
def update_system_config(
    data: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    config = db.query(SystemConfig).filter(SystemConfig.config_key == data.key).first()
    if not config:
        config = SystemConfig(config_key=data.key, config_value=data.value)
        db.add(config)
    else:
        config.config_value = data.value
    db.commit()
    logger.info(f"Config updated: key={data.key} by admin={current_user.username}")
    return {"message": f"Config '{data.key}' updated"}


@router.put("/developer/config")
def update_developer_system_config(
    data: SystemConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    return update_system_config(data=data, db=db, current_user=current_user)


@router.get("/deposit-bonus/config", response_model=DepositBonusConfigResponse)
def get_admin_deposit_bonus_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    return get_deposit_bonus_config(db)


@router.put("/deposit-bonus/config", response_model=DepositBonusConfigResponse)
def update_admin_deposit_bonus_config(
    data: DepositBonusConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    updated_payload = set_deposit_bonus_config(db, data.model_dump())
    db.commit()
    logger.info("Deposit bonus config updated by admin=%s", current_user.username)
    return updated_payload


@router.get("/referral-reward/config", response_model=ReferralRewardConfigResponse)
def get_admin_referral_reward_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    return get_referral_reward_config(db)

# ── Home Popup Management ───────────────────────────────────

@router.get("/home-popups", response_model=List[HomePopupResponse])
def list_home_popups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    return db.query(HomePopup).order_by(HomePopup.id.desc()).all()

@router.post("/home-popups", response_model=HomePopupResponse)
def create_home_popup(
    data: HomePopupCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = HomePopup(**data.model_dump())
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    logger.info(f"Home popup created: title={db_obj.title} by admin={current_user.username}")
    return db_obj

@router.put("/home-popups/{popup_id}", response_model=HomePopupResponse)
def update_home_popup(
    popup_id: int,
    data: HomePopupCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = db.query(HomePopup).filter(HomePopup.id == popup_id).first()
    if not db_obj:
        raise HTTPException(status_code=404, detail="Popup not found")
    
    for field, value in data.model_dump().items():
        setattr(db_obj, field, value)
    
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    logger.info(f"Home popup updated: id={popup_id} by admin={current_user.username}")
    return db_obj

@router.delete("/home-popups/{popup_id}")
def delete_home_popup(
    popup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = db.query(HomePopup).filter(HomePopup.id == popup_id).first()
    if not db_obj:
        raise HTTPException(status_code=404, detail="Popup not found")
    
    db.delete(db_obj)
    db.commit()
    logger.info(f"Home popup deleted: id={popup_id} by admin={current_user.username}")
    return {"message": "Popup deleted successfully"}


@router.put("/referral-reward/config", response_model=ReferralRewardConfigResponse)
def update_admin_referral_reward_config(
    data: ReferralRewardConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    updated_payload = set_referral_reward_config(db, data.model_dump())
    db.commit()
    logger.info("Referral reward config updated by admin=%s", current_user.username)
    return updated_payload


# ─────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────

@router.post("/notifications/send")
def send_push_notification(
    data: NotificationSendRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    target_user_ids: list[int] = []
    if data.user_ids:
        target_user_ids = sorted({uid for uid in data.user_ids if isinstance(uid, int) and uid > 0})

    users_query = db.query(User).filter(User.role == "USER", User.is_active == True)
    if target_user_ids:
        users_query = users_query.filter(User.id.in_(target_user_ids))

    users = users_query.all()
    if not users:
        raise HTTPException(status_code=404, detail="No active users found for notification target.")

    display_title = append_firebase_suffix(data.title, max_length=100)
    display_body = append_firebase_suffix(data.body)

    tokens = []
    for user in users:
        notif = Notification(
            user_id=user.id,
            title=display_title,
            content=display_body,
            type="SYSTEM"
        )
        db.add(notif)
        if user.fcm_token:
            tokens.append(user.fcm_token)

    # Normalize and dedupe tokens to avoid duplicate FCM sends.
    tokens = list(dict.fromkeys([token.strip() for token in tokens if token and token.strip()]))

    db.commit()

    push_sent = 0
    push_total = len(tokens)
    invalid_tokens_cleared = 0

    if tokens:
        if target_user_ids:
            # Targeted sends run synchronously so we can capture delivery failures immediately
            # and clear stale tokens in the same request.
            push_result = send_push_to_many_detailed(
                fcm_tokens=tokens,
                title=display_title,
                body=display_body,
                data={"type": "SYSTEM"},
            )
            push_sent = int(push_result.get("success_count", 0))

            invalid_tokens = [t for t in push_result.get("invalid_tokens", []) if t]
            if invalid_tokens:
                invalid_tokens_cleared = (
                    db.query(User)
                    .filter(User.fcm_token.in_(invalid_tokens))
                    .update({User.fcm_token: None}, synchronize_session=False)
                )
                db.commit()
        else:
            background_tasks.add_task(
                send_push_to_many,
                fcm_tokens=tokens,
                title=display_title,
                body=display_body,
                data={"type": "SYSTEM"}
            )

    target_mode = "targeted" if target_user_ids else "broadcast"
    target_label = f"{len(users)} selected users" if target_user_ids else f"{len(users)} users"
    if target_mode == "targeted":
        push_label = f"{push_sent}/{push_total} via Push"
        if invalid_tokens_cleared:
            push_label = f"{push_label}, {invalid_tokens_cleared} stale token(s) cleared"
    else:
        push_label = f"{push_total} via Push queued"

    logger.info(
        f"Notification ({target_mode}) sent to {target_label} by admin={current_user.username}: '{display_title}' ({push_label})"
    )

    return {
        "message": f"Notification '{display_title}' scheduled for {target_label} ({push_label})",
        "target_mode": target_mode,
        "users_count": len(users),
        "push_total": push_total,
        "push_sent": push_sent if target_mode == "targeted" else None,
        "invalid_tokens_cleared": invalid_tokens_cleared,
    }


@router.post("/developer/notifications/send")
def send_developer_push_notification(
    data: NotificationSendRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    return send_push_notification(data=data, background_tasks=background_tasks, db=db, current_user=current_user)


# ─────────────────────────────────────────────────────────────────
# Transaction audit log — FIXED: N+1 query resolved with JOIN
# ─────────────────────────────────────────────────────────────────

@router.get("/transactions")
def list_all_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
    status: str = "",
    type: str = "",
    search: str = "",
    limit: int = 100
):
    """Full transaction audit log — all types, all statuses, all users."""
    safe_limit = max(1, min(limit, 500))
    q = db.query(WalletTransaction).order_by(WalletTransaction.created_at.desc())

    if status:
        q = q.filter(WalletTransaction.status == status.upper())
    if type:
        q = q.filter(WalletTransaction.transaction_type == type.upper())

    fetch_limit = safe_limit * 3 if search else safe_limit
    txs = q.limit(fetch_limit).all()

    # FIXED: Bulk-load all needed users in one query (eliminates N+1)
    user_ids = list({tx.user_id for tx in txs})
    users = {}
    if user_ids:
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    withdraw_accounts_by_user: dict[int, list[WithdrawUpiAccount]] = {}
    if user_ids:
        withdraw_accounts = (
            db.query(WithdrawUpiAccount)
            .filter(WithdrawUpiAccount.user_id.in_(user_ids))
            .order_by(WithdrawUpiAccount.id.desc())
            .all()
        )
        for account in withdraw_accounts:
            withdraw_accounts_by_user.setdefault(account.user_id, []).append(account)

    res = []
    for tx in txs:
        u        = users.get(tx.user_id)
        username = u.username if u else "Unknown"
        email    = u.email    if u else ""
        phone    = u.phone_number if u else None
        user_accounts = withdraw_accounts_by_user.get(tx.user_id, [])
        upi_id   = user_accounts[0].upi_id if user_accounts else None
        is_withdrawal = tx.transaction_type == "WITHDRAWAL"

        withdrawal_upi_id = None
        withdrawal_account_holder_name = None
        if is_withdrawal:
            withdrawal_upi_id = (
                getattr(tx, 'payu_txn_id', None)
                or getattr(tx, 'gateway_payment_id', None)
                or upi_id
            )

            matched_account = None
            if withdrawal_upi_id:
                normalized_withdraw_upi = str(withdrawal_upi_id).strip().lower()
                for account in user_accounts:
                    if (account.upi_id or "").strip().lower() == normalized_withdraw_upi:
                        matched_account = account
                        break

            if not matched_account and upi_id:
                normalized_user_upi = str(upi_id).strip().lower()
                for account in user_accounts:
                    if (account.upi_id or "").strip().lower() == normalized_user_upi:
                        matched_account = account
                        break

            withdrawal_account_holder_name = (
                matched_account.account_holder_name
                if matched_account
                else (username if withdrawal_upi_id else None)
            )

        if search:
            search_lower = search.lower()
            if not any([
                search_lower in username.lower(),
                search_lower in email.lower(),
                search_lower in (phone or "").lower(),
                search_lower in (upi_id or "").lower(),
                search_lower in (withdrawal_upi_id or "").lower(),
                search_lower in (withdrawal_account_holder_name or "").lower(),
                search_lower in (tx.reference_id or "").lower(),
                search_lower in (tx.payu_txn_id or "").lower(),
                search_lower in (getattr(tx, 'gateway_order_id', None) or "").lower(),
                search_lower in (getattr(tx, 'gateway_payment_id', None) or "").lower(),
                search_lower in str(tx.user_id),
            ]):
                continue

        res.append({
            "id":             tx.id,
            "user_id":        tx.user_id,
            "username":       username,
            "email":          email,
            "phone_number":   phone,
            "user_upi_id":    upi_id,
            "user_role":      u.role if u else None,
            "user_profile_pic": u.profile_pic if u else None,
            "user_is_active": u.is_active if u else None,
            "user_wallet_balance": float(u.wallet_balance) if (u and u.wallet_balance is not None) else None,
            "user_referral_code": u.referral_code if u else None,
            "bgmi_id":        None,
            "freefire_id":    u.freefire_id if u else None,
            "valorant_id":    None,
            "user_created_at": u.created_at if u else None,
            "user_updated_at": u.updated_at if u else None,
            "amount":         float(tx.amount),
            "type":           tx.transaction_type,
            "status":         tx.status,
            "reference_id":   tx.reference_id,
            "payu_txn_id":    getattr(tx, 'payu_txn_id', None),
            "gateway_utr":    getattr(tx, 'payu_txn_id', None) if tx.transaction_type == "ADD_MONEY" else None,
            "payment_mode":   "UPI" if is_withdrawal else getattr(tx, 'payment_mode', None),
            "failure_reason": getattr(tx, 'failure_reason', None),
            "gateway_order_id": None if is_withdrawal else getattr(tx, 'gateway_order_id', None),
            "gateway_payment_id": None if is_withdrawal else getattr(tx, 'gateway_payment_id', None),
            "gateway_signature": None if is_withdrawal else getattr(tx, 'gateway_signature', None),
            "withdrawal_upi_id": withdrawal_upi_id if is_withdrawal else None,
            "withdrawal_account_holder_name": withdrawal_account_holder_name if is_withdrawal else None,
            "created_at":     tx.created_at,
            "updated_at":     tx.updated_at,
        })
        if len(res) >= safe_limit:
            break

    return res


# ─────────────────────────────────────────────────────────────────
# Finance stats
# ─────────────────────────────────────────────────────────────────

@router.get("/finance-stats")
def get_finance_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_finance = _get_today_finance_metrics(db)

    total_recharged_today = float(db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.created_at >= today_start
    ).scalar() or 0.0)

    failed_today = db.query(func.count(WalletTransaction.id)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "FAILED",
        WalletTransaction.created_at >= today_start
    ).scalar() or 0

    pending_payments = db.query(func.count(WalletTransaction.id)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "PENDING"
    ).scalar() or 0

    pending_withdrawals = db.query(func.count(WalletTransaction.id)).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "PENDING"
    ).scalar() or 0

    total_recharged_all = float(db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0)

    return {
        "total_recharged_today":    round(total_recharged_today, 2),
        "failed_today":             failed_today,
        "pending_payments":         pending_payments,
        "pending_withdrawals":      pending_withdrawals,
        "total_recharged_all_time": round(total_recharged_all, 2),
        "daily_recharged_today": today_finance["daily_recharged_today"],
        "daily_withdrawal_requested_today": today_finance["daily_withdrawal_requested_today"],
        "daily_withdrawal_success_today": today_finance["daily_withdrawal_success_today"],
    }


# ─────────────────────────────────────────────────────────────────
# Manual transaction management
# ─────────────────────────────────────────────────────────────────

@router.post("/transactions/{transaction_id}/manual-credit")
def manual_credit_transaction(
    transaction_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.id == transaction_id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.transaction_type != "ADD_MONEY":
        raise HTTPException(status_code=400, detail="Manual approve is allowed only for ADD_MONEY transactions")
    if tx.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Transaction is already {tx.status}")

    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    credit_amount = Decimal(tx.amount or Decimal("0.00"))
    if credit_amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Invalid add-money amount")

    credit_wallet(user, credit_amount, WALLET_BUCKET_DEPOSIT)
    tx.status = "SUCCESS"
    tx.payment_mode = tx.payment_mode or "MANUAL_APPROVE"
    tx.failure_reason = None

    deposit_bonus = apply_deposit_bonus_if_eligible(
        db=db,
        user=user,
        deposit_tx=tx,
        source="ADMIN_MANUAL_CREDIT",
    )

    db.add(tx)
    db.add(user)
    db.commit()

    try:
        add_user_notification(
            db, tx.user_id,
            "Payment Confirmed ✅",
            (
                f"₹{float(credit_amount):.0f} has been added to your GamerzAdda wallet."
                if deposit_bonus <= Decimal("0.00")
                else (
                    f"₹{float(credit_amount):.0f} added + ₹{float(deposit_bonus):.2f} "
                    "deposit bonus credited to your wallet."
                )
            ),
            "WALLET"
        )
    except Exception:
        pass

    logger.warning(
        f"Manual credit approved by admin={current_user.username} for tx={transaction_id} "
        f"user={tx.user_id} amount={float(credit_amount):.2f} bonus={float(deposit_bonus):.2f}"
    )
    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {
        "message": f"Transaction #{transaction_id} approved and credited.",
        "deposit_bonus": float(deposit_bonus),
    }


@router.post("/transactions/{transaction_id}/mark-failed")
def mark_transaction_failed(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.id == transaction_id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Transaction is already {tx.status}")
    if tx.transaction_type not in ("ADD_MONEY", "WITHDRAWAL"):
        raise HTTPException(status_code=400, detail="Only ADD_MONEY or WITHDRAWAL can be marked failed")

    tx.status         = "FAILED"

    refunded = Decimal("0.00")
    if tx.transaction_type == "WITHDRAWAL":
        refunded = _refund_withdrawal_if_needed(
            db,
            tx,
            current_user.username,
            "MARKED_FAILED_BY_ADMIN",
        )
    else:
        tx.failure_reason = f"MARKED_FAILED_BY_ADMIN:{current_user.username}"

    db.add(tx)
    db.commit()

    # NOTIFY USER
    try:
        add_user_notification(
            db, tx.user_id,
            "Transaction Failed ❌",
            f"Your transaction #{transaction_id} which was PENDING has been marked as failed by the administrator.",
            "WALLET"
        )
    except Exception: pass

    logger.info(
        f"Transaction {transaction_id} marked FAILED by admin={current_user.username}; "
        f"refund={float(refunded):.2f}"
    )
    return {"message": f"Transaction #{transaction_id} marked as FAILED."}


# ─────────────────────────────────────────────────────────────────
# Leaderboard & Bans
# ─────────────────────────────────────────────────────────────────

@router.get("/leaderboard")
def get_leaderboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    users = db.query(User).filter(
        User.role == 'USER'
    ).order_by(User.wallet_balance.desc()).limit(50).all()
    return [
        {"id": u.id, "username": u.username, "balance": float(u.wallet_balance), "is_active": u.is_active}
        for u in users
    ]


@router.get("/banned_users")
def get_banned_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    users = db.query(User).filter(User.is_active == False).all()
    user_ids = [u.id for u in users]

    restrictions = db.query(UserRestriction).filter(
        UserRestriction.user_id.in_(user_ids),
        UserRestriction.scope == RESTRICTION_SCOPE_FULL_APP,
        UserRestriction.is_active == True,
    ).order_by(UserRestriction.created_at.desc()).all() if user_ids else []

    latest_restriction_by_user: dict[int, UserRestriction] = {}
    for restriction in restrictions:
        if restriction.user_id in latest_restriction_by_user:
            continue
        if not is_restriction_currently_active(restriction):
            continue
        latest_restriction_by_user[restriction.user_id] = restriction

    active_locks = db.query(OtpPhoneLock).filter(
        OtpPhoneLock.user_id.in_(user_ids),
        OtpPhoneLock.is_locked == True,
    ).all() if user_ids else []
    lock_by_user = {lock.user_id: lock for lock in active_locks if lock.user_id}

    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "phone_number": u.phone_number,
            "role": u.role,
            "is_active": bool(u.is_active),
            "balance": float(u.wallet_balance),
            "created_at": u.created_at,
            "last_login_ip": u.last_login_ip,
            "last_login_device": u.last_login_device,
            "restricted_reason": (
                (latest_restriction_by_user.get(u.id).reason if latest_restriction_by_user.get(u.id) else None)
                or (lock_by_user.get(u.id).lock_reason if lock_by_user.get(u.id) else None)
            ),
            "restricted_at": (
                (latest_restriction_by_user.get(u.id).starts_at if latest_restriction_by_user.get(u.id) else None)
                or (lock_by_user.get(u.id).locked_at if lock_by_user.get(u.id) else None)
            ),
            "otp_send_count": lock_by_user.get(u.id).otp_send_count if lock_by_user.get(u.id) else None,
            "otp_lock_id": lock_by_user.get(u.id).id if lock_by_user.get(u.id) else None,
        }
        for u in users
    ]


def _serialize_admin_otp_lock(lock: OtpPhoneLock, user: User | None) -> dict:
    return {
        "id": lock.id,
        "phone_number": lock.phone_number,
        "user_id": lock.user_id,
        "username": user.username if user else None,
        "email": user.email if user else None,
        "user_is_active": bool(user.is_active) if user else None,
        "otp_send_count": int(lock.otp_send_count or 0),
        "is_locked": bool(lock.is_locked),
        "lock_reason": lock.lock_reason,
        "last_source": lock.last_source,
        "first_sent_at": lock.first_sent_at,
        "last_sent_at": lock.last_sent_at,
        "locked_at": lock.locked_at,
        "unlocked_at": lock.unlocked_at,
        "reset_note": lock.reset_note,
    }


def _serialize_admin_activity_lock(lock: UserActivityLock, user: User | None) -> dict:
    return {
        "id": lock.id,
        "user_id": lock.user_id,
        "username": user.username if user else None,
        "email": user.email if user else None,
        "user_is_active": bool(user.is_active) if user else None,
        "activity_type": lock.activity_type,
        "cycle_key": lock.cycle_key,
        "daily_count": int(lock.daily_count or 0),
        "failed_streak": int(lock.failed_streak or 0),
        "is_locked": bool(lock.is_locked),
        "lock_status": lock.lock_status,
        "lock_reason": lock.lock_reason,
        "locked_at": lock.locked_at,
        "lock_expires_at": lock.lock_expires_at,
        "last_attempt_at": lock.last_attempt_at,
        "last_success_at": lock.last_success_at,
        "unlocked_at": lock.unlocked_at,
        "reset_note": lock.reset_note,
    }


@router.get("/otp-locks")
def get_otp_locks(
    include_unlocked: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    locks = list_otp_locks_sync(db, include_unlocked=include_unlocked)
    user_ids = {lock.user_id for lock in locks if lock.user_id}
    users_by_id = {
        item.id: item
        for item in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    return [
        _serialize_admin_otp_lock(lock, users_by_id.get(lock.user_id or 0))
        for lock in locks
    ]


@router.post("/otp-locks/{lock_id}/reset")
def reset_otp_lock(
    lock_id: int,
    payload: OtpLockResetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    lock = db.query(OtpPhoneLock).filter(OtpPhoneLock.id == lock_id).with_for_update().first()
    if not lock:
        raise HTTPException(status_code=404, detail="OTP lock not found")

    updated = reset_otp_lock_sync(
        db,
        lock=lock,
        admin_id=current_user.id,
        note=(payload.note or "").strip() or "OTP limit reset from admin panel",
    )

    user = db.query(User).filter(User.id == updated.user_id).first() if updated.user_id else None
    return {
        "message": "OTP limit reset successfully",
        "lock": _serialize_admin_otp_lock(updated, user),
    }


@router.get("/activity-locks")
def get_activity_locks(
    include_unlocked: bool = False,
    activity_type: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    locks = list_activity_locks_sync(
        db,
        include_unlocked=include_unlocked,
        activity_type=activity_type,
    )
    user_ids = {lock.user_id for lock in locks if lock.user_id}
    users_by_id = {
        item.id: item
        for item in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    return [
        _serialize_admin_activity_lock(lock, users_by_id.get(lock.user_id))
        for lock in locks
    ]


@router.post("/activity-locks/{lock_id}/reset")
def reset_activity_lock(
    lock_id: int,
    payload: ActivityLockResetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    lock = db.query(UserActivityLock).filter(UserActivityLock.id == lock_id).with_for_update().first()
    if not lock:
        raise HTTPException(status_code=404, detail="Activity lock not found")

    updated = reset_activity_lock_sync(
        db,
        lock=lock,
        admin_id=current_user.id,
        note=(payload.note or "").strip() or "Activity limit reset from admin panel",
    )

    user = db.query(User).filter(User.id == updated.user_id).first()
    return {
        "message": "Activity lock reset successfully",
        "lock": _serialize_admin_activity_lock(updated, user),
    }

@router.post("/transactions/reject-all-pending")
def reject_all_pending_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    """Mark all currently PENDING ADD_MONEY/WITHDRAWAL transactions as FAILED with safe refunds."""
    pending = db.query(WalletTransaction).filter(
        WalletTransaction.status == "PENDING",
        WalletTransaction.transaction_type.in_(("ADD_MONEY", "WITHDRAWAL")),
    ).with_for_update().all()

    affected = 0
    refund_count = 0
    refund_total = Decimal("0.00")

    for tx in pending:
        tx.status = "FAILED"
        if tx.transaction_type == "WITHDRAWAL":
            refunded = _refund_withdrawal_if_needed(
                db,
                tx,
                current_user.username,
                "REJECTED_BY_ADMIN_BULK",
            )
            if refunded > Decimal("0.00"):
                refund_count += 1
                refund_total += refunded
        else:
            tx.failure_reason = f"REJECTED_BY_ADMIN_BULK:{current_user.username}"
        db.add(tx)
        affected += 1

    db.commit()
    logger.info(
        f"Admin {current_user.username} rejected pending transactions. "
        f"Affected={affected}, refunded_withdrawals={refund_count}, refund_total={float(refund_total):.2f}"
    )
    return {
        "message": f"Successfully rejected {affected} pending transactions",
        "refunded_withdrawals": refund_count,
        "refund_total": float(refund_total),
    }


@router.post("/transactions/clear-history")
def clear_transaction_history(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    """Hard clear transaction ledger after refunding pending withdrawals safely."""
    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.status == "PENDING",
        WalletTransaction.transaction_type == "WITHDRAWAL",
    ).with_for_update().all()

    refunded_count = 0
    refunded_total = Decimal("0.00")

    for tx in pending_withdrawals:
        refunded = _refund_withdrawal_if_needed(
            db,
            tx,
            current_user.username,
            "CLEAR_HISTORY",
        )
        if refunded > Decimal("0.00"):
            refunded_count += 1
            refunded_total += refunded

    deleted_count = db.query(WalletTransaction).delete(synchronize_session=False)
    db.commit()

    logger.warning(
        f"Admin {current_user.username} cleared transaction history. "
        f"deleted={deleted_count}, refunded_withdrawals={refunded_count}, "
        f"refund_total={float(refunded_total):.2f}"
    )

    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {
        "message": f"Cleared {deleted_count} ledger entries",
        "deleted": deleted_count,
        "refunded_withdrawals": refunded_count,
        "refund_total": float(refunded_total),
    }


@router.post("/run-bonus-expiry", tags=["admin"])
def run_bonus_expiry(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Manually trigger the bonus expiry cycle.
    Processes expired bonuses and sends reminders.
    """
    from services.bonus_expiry import run_bonus_expiry_cycle

    result = run_bonus_expiry_cycle(db)
    return {
        "message": "Bonus expiry cycle completed",
        "expired": result["expired"],
        "reminders": result["reminders"],
    }


# ------------------------------------------------------------------------------
# Sub-Admin Management
# ------------------------------------------------------------------------------

def _ensure_super_admin(current_user: User):
    """Ensure the user has super-admin rights. For now, empty or '*' permissions, or specific phone number."""
    # Assuming primary phone number or empty permissions means super admin
    if current_user.phone_number == settings.ADMIN_LOGIN_PHONE:
        return
    if not current_user.admin_permissions or current_user.admin_permissions == "*":
        return
    raise HTTPException(status_code=403, detail="Super Admin access required")


@router.get("/sub-admins", response_model=List[UserResponse])
def get_sub_admins(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    _ensure_super_admin(current_user)
    # Exclude the super admin themselves or users without role='ADMIN'
    admins = db.query(User).filter(User.role == "ADMIN").all()
    return admins


@router.post("/sub-admins", response_model=UserResponse)
def create_sub_admin(
    payload: SubAdminCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    _ensure_super_admin(current_user)
    
    # Check if user already exists
    user = db.query(User).filter(User.phone_number == payload.phone_number).first()
    
    if not user:
        # Create a new user record
        user = User(
            username=payload.name,
            email=payload.email,
            phone_number=payload.phone_number,
            password_hash=hash_password(payload.password),
            role="ADMIN",
            admin_permissions=payload.admin_permissions
        )
        db.add(user)
    else:
        # Promote existing user
        user.role = "ADMIN"
        user.admin_permissions = payload.admin_permissions
        user.password_hash = hash_password(payload.password)
        
    db.commit()
    db.refresh(user)
    return user


@router.put("/sub-admins/{user_id}", response_model=UserResponse)
def update_sub_admin(
    user_id: int,
    payload: SubAdminUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    _ensure_super_admin(current_user)
    
    user = db.query(User).filter(User.id == user_id, User.role == "ADMIN").first()
    if not user:
        raise HTTPException(status_code=404, detail="Sub-admin not found")
        
    if user.phone_number == settings.ADMIN_LOGIN_PHONE:
        raise HTTPException(status_code=403, detail="Cannot modify the Super Admin")
        
    if payload.admin_permissions is not None:
        user.admin_permissions = payload.admin_permissions
    if payload.name:
        user.username = payload.name
    if payload.email:
        user.email = payload.email
    if payload.password:
        user.password_hash = hash_password(payload.password)
        
    db.commit()
    db.refresh(user)
    return user


@router.delete("/sub-admins/{user_id}")
def delete_sub_admin(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    _ensure_super_admin(current_user)
    
    user = db.query(User).filter(User.id == user_id, User.role == "ADMIN").first()
    if not user:
        raise HTTPException(status_code=404, detail="Sub-admin not found")
        
    if user.phone_number == settings.ADMIN_LOGIN_PHONE:
        raise HTTPException(status_code=403, detail="Cannot revoke the Super Admin")
        
    try:
        from sqlalchemy import text as _t

        deleted_username = user.username
        deleted_email    = user.email

        db.flush()  # push any pending ORM state before raw SQL
        uid = user_id

        # PHASE 1 — SET NULL on every nullable FK column referencing this admin
        db.execute(_t("UPDATE chat_sessions SET attended_by_admin_id = NULL WHERE attended_by_admin_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_sessions SET blocked_by_admin_id  = NULL WHERE blocked_by_admin_id  = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_sessions SET ended_by_user_id     = NULL WHERE ended_by_user_id     = :uid"), {"uid": uid})

        db.execute(_t("UPDATE chat_messages SET thread_user_id = NULL WHERE thread_user_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE chat_messages SET sender_id      = NULL WHERE sender_id      = :uid"), {"uid": uid})

        db.execute(_t("UPDATE user_restrictions SET created_by_admin_id = NULL WHERE created_by_admin_id = :uid"), {"uid": uid})
        db.execute(_t("UPDATE user_restrictions SET lifted_by_admin_id  = NULL WHERE lifted_by_admin_id  = :uid"), {"uid": uid})

        db.execute(_t("UPDATE user_activity_locks SET unlocked_by_admin_id = NULL WHERE unlocked_by_admin_id = :uid"), {"uid": uid})

        db.execute(_t("UPDATE otp_phone_locks SET user_id              = NULL WHERE user_id              = :uid"), {"uid": uid})
        db.execute(_t("UPDATE otp_phone_locks SET unlocked_by_admin_id = NULL WHERE unlocked_by_admin_id = :uid"), {"uid": uid})

        db.execute(_t("UPDATE users SET referred_by_id = NULL WHERE referred_by_id = :uid"), {"uid": uid})

        # PHASE 2 — DELETE child rows where user_id is NOT NULL
        db.execute(_t("""
            DELETE FROM chat_messages
            WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_id = :uid)
        """), {"uid": uid})

        db.execute(_t("DELETE FROM chat_sessions WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM notifications WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM tournament_participants WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM wallet_transactions WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM user_restrictions WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM user_activity_locks WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM withdraw_upi_accounts WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM email_otp_logs WHERE user_id = :uid"), {"uid": uid})
        db.execute(_t("DELETE FROM admin_access_sessions WHERE user_id = :uid"), {"uid": uid})

        # PHASE 3 — Delete the user itself
        profile_pic_url = user.profile_pic
        db.execute(_t("DELETE FROM users WHERE id = :uid"), {"uid": uid})
        db.commit()

        # PHASE 4 — Cleanup profile picture from disk
        if profile_pic_url and "/static/profile_pics/" in profile_pic_url:
            try:
                import os
                filename = profile_pic_url.rsplit("/", 1)[-1]
                path = os.path.join("static/profile_pics", filename)
                if os.path.isfile(path):
                    os.remove(path)
            except Exception as e:
                logger.warning(f"Failed to delete profile pic file for user {user_id}: {e}")

        logger.warning(f"Sub-Admin fully deleted by Super Admin {current_user.username}: id={user_id}, username={deleted_username}")

        return {"message": "Sub-admin account fully deleted successfully"}

    except Exception as e:
        db.rollback()
        logger.error(f"Error fully deleting sub-admin: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete sub-admin from database")
