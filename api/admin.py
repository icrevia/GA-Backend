from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, BackgroundTasks, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List
from decimal import Decimal, ROUND_HALF_UP
import uuid
import os
import uuid
import logging
import hashlib
import json
import secrets
from threading import Lock
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib import request as urllib_request

from api.deps import get_db, get_current_active_admin
from core.config import settings
from models.user import User
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.config import SystemConfig
from models.notification import Notification
from models.participant import TournamentParticipant
from models.support import ChatSession, ChatMessage
from services.notifications import add_user_notification
from core.websockets import manager as ws_manager

from schemas.admin import (
    SystemConfigUpdate,
    NotificationSendRequest,
    UserStatusUpdate,
    TournamentRoomUpdate,
    TournamentConclude,
    TournamentCreateAdmin,
    DeveloperOtpRequestResponse,
    DeveloperOtpVerifyRequest,
    DeveloperOtpVerifyResponse,
    DeveloperOtpStatusResponse,
)
from schemas.tournament import TournamentCreate, TournamentResponse

logger = logging.getLogger("zexplay.admin")
router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# APK Upload — FIXED: path traversal prevention + size cap
# ─────────────────────────────────────────────────────────────────

MAX_APK_SIZE_MB = 150
MAX_APK_SIZE_BYTES = MAX_APK_SIZE_MB * 1024 * 1024

_DEVELOPER_OTP_LOCK = Lock()
_DEVELOPER_OTP_STATE: dict[int, dict[str, object]] = {}
_DEVELOPER_OTP_SESSIONS: dict[str, tuple[int, datetime]] = {}


def _developer_otp_chat_id() -> str:
    configured = (settings.DEVELOPER_OTP_TELEGRAM_CHAT_ID or "").strip()
    if configured:
        return configured
    return (settings.TELEGRAM_ALERT_CHAT_ID or "").strip()


def _otp_digest(admin_id: int, otp: str) -> str:
    payload = f"{settings.SECRET_KEY}|developer-otp|{admin_id}|{otp}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generate_numeric_otp(length: int) -> str:
    min_value = 10 ** (length - 1)
    max_value = (10 ** length) - 1
    return str(secrets.randbelow(max_value - min_value + 1) + min_value)


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
    chat_id = _developer_otp_chat_id()
    if not bot_token or not chat_id:
        raise HTTPException(
            status_code=503,
            detail="Developer OTP delivery is not configured. Set TELEGRAM_BOT_TOKEN and DEVELOPER_OTP_TELEGRAM_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID).",
        )

    client_ip = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    client_ip = client_ip.split(",")[0].strip() if client_ip else "unknown"

    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    message = "\n".join([
        "=== ZexPlay Developer OTP ===",
        f"Admin ID: {admin.id}",
        f"Username: {admin.username}",
        f"OTP: {otp}",
        f"Valid For (seconds): {settings.DEVELOPER_OTP_TTL_SECONDS}",
        f"Requested At (UTC): {now_utc}",
        f"Request IP: {client_ip}",
        "Never share this code with anyone.",
    ])[:4096]

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
                logger.warning("Developer OTP telegram send failed: status=%s body=%s", resp.status, body)
                raise HTTPException(status_code=503, detail="Failed to deliver OTP. Please retry.")
    except HTTPError as exc:
        logger.warning("Developer OTP telegram HTTPError: status=%s", exc.code)
        raise HTTPException(status_code=503, detail="Failed to deliver OTP. Please retry.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Developer OTP telegram error: %s", exc)
        raise HTTPException(status_code=503, detail="Failed to deliver OTP. Please retry.")


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
        for token, (admin_id, _) in list(_DEVELOPER_OTP_SESSIONS.items()):
            if admin_id == current_user.id:
                _DEVELOPER_OTP_SESSIONS.pop(token, None)

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

    # FIXED: Validate by extension (basic check)
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="Only APK files are allowed.")

    # FIXED: Generate a safe server-side filename — never use user-supplied name
    safe_filename = f"zexplay_app_{uuid.uuid4().hex}.apk"
    static_dir = "static"
    os.makedirs(static_dir, exist_ok=True)
    file_path = os.path.join(static_dir, safe_filename)

    try:
        # Read in chunks to enforce size cap without loading into memory
        written = 0
        with open(file_path, "wb") as buffer:
            while chunk := file.file.read(1024 * 1024):  # 1 MB chunks
                written += len(chunk)
                if written > MAX_APK_SIZE_BYTES:
                    os.remove(file_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"APK file exceeds {MAX_APK_SIZE_MB} MB size limit."
                    )
                buffer.write(chunk)

        logger.info(f"APK uploaded by admin={admin.username}: {safe_filename} ({written / (1024*1024):.1f} MB)")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"APK upload failed: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail="Failed to save file.")

    return {"url": f"/static/{safe_filename}", "filename": safe_filename}


@router.post("/developer/config/upload-apk")
def upload_apk_developer(
    file: UploadFile = File(...),
    admin: User = Depends(require_developer_otp),
):
    return upload_apk(file=file, admin=admin)


# ─────────────────────────────────────────────────────────────────
# Tournament management
# ─────────────────────────────────────────────────────────────────

@router.get("/tournaments", response_model=List[TournamentResponse])
def list_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from api.tournaments import _with_count
    tournaments = db.query(Tournament).order_by(Tournament.created_at.desc()).all()
    return [_with_count(t, db) for t in tournaments]


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
        match_type=data.match_type,
        match_time=dt,
        game_image_url=data.game_image_url,
        max_slots=data.max_slots or 100,
        status="UPCOMING"
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return _with_count(db_obj, db)


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
                f"Room ID and Password for '{db_obj.title}' are now available in the app. Join quickly!",
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
    winner_id = int(data.winner_id) if isinstance(data.winner_id, str) else data.winner_id
    if not winner_id:
        raise HTTPException(status_code=422, detail="Winner ID is required")

    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")

    # FIXED: Validate winner is actually a participant
    participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == winner_id
    ).first()
    if not participant:
        raise HTTPException(status_code=400, detail="Winner must be a registered participant in this tournament")

    winner = db.query(User).filter(User.id == winner_id).with_for_update().first()
    if not winner:
        raise HTTPException(status_code=404, detail="Winner user not found")

    prize = tournament.prize_pool
    winner.wallet_balance += prize

    win_tx = WalletTransaction(
        user_id=winner.id,
        amount=prize,
        transaction_type="PRIZE_WIN",
        status="SUCCESS",
        reference_id=f"WIN_TRN_{tournament_id}"
    )
    db.add(win_tx)
    db.add(winner)
    tournament.winner_id = winner_id
    tournament.status    = "COMPLETED"
    db.add(tournament)
    db.commit()

    try:
        add_user_notification(
            db,
            winner.id,
            "CHAMPION! 🏆",
            f"You won ₹{prize} in {tournament.title}! Check your wallet.",
            "APP"
        )
    except Exception:
        pass

    logger.info(f"Tournament {tournament_id} concluded. Winner: {winner_id}, Prize: ₹{prize}")
    
    # BROADCAST TO ALL PARTICIPANTS
    try:
        parts = db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).all()
        for p in parts:
            if p.user_id != winner_id: # Winner already gets a notification
                add_user_notification(
                    db, p.user_id,
                    "Tournament Completed 🏆",
                    f"'{tournament.title}' has ended. Check the results in the app. Better luck next time!",
                    "APP"
                )
    except Exception: pass

    return {"message": f"Tournament concluded. Winner paid ₹{prize}"}


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
    ).all()

    ref_count = 0
    for p in participants:
        user = db.query(User).filter(User.id == p.user_id).with_for_update().first()
        if user:
            user.wallet_balance += tournament.entry_fee
            ref_tx = WalletTransaction(
                user_id=user.id,
                amount=tournament.entry_fee,
                transaction_type="REFUND",
                status="SUCCESS",
                reference_id=f"REFUND_{tournament_id}_{user.id}"
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

    estimated_revenue = total_revenue_pool - float(total_prizes)

    # NEW: Pending Withdrawals count
    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "PENDING"
    ).count()

    # NEW: Daily Revenue for Chart (Last 7 Days)
    # We group by date of created_at
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
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
    days_map = { (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d"): 0.0 for i in range(7) }
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
    """Refund a pending withdrawal exactly once and write an immutable refund ledger entry."""
    if tx.transaction_type != "WITHDRAWAL":
        return Decimal("0.00")

    refund_reference = f"REFUND_WD_{tx.id}"
    existing_refund = db.query(WalletTransaction).filter(
        WalletTransaction.reference_id == refund_reference
    ).first()
    if existing_refund:
        return Decimal("0.00")

    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for refund")

    refund_amount = abs(Decimal(tx.amount or Decimal("0.00")))
    if refund_amount <= Decimal("0.00"):
        return Decimal("0.00")

    user.wallet_balance = (user.wallet_balance or Decimal("0.00")) + refund_amount
    refund_tx = WalletTransaction(
        user_id=tx.user_id,
        amount=refund_amount,
        transaction_type="WITHDRAWAL_REFUND",
        status="SUCCESS",
        reference_id=refund_reference,
        payment_mode="SYSTEM_REFUND",
        failure_reason=f"SOURCE_WITHDRAWAL:{tx.id};REASON:{reason};ADMIN:{admin_username}",
    )
    db.add(user)
    db.add(refund_tx)
    tx.failure_reason = f"{reason}|REFUNDED:{refund_reference}"
    return refund_amount

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

    return [
        {
            "id":         tx.id,
            "user_id":    tx.user_id,
            "username":   users[tx.user_id].username if tx.user_id in users else "Unknown",
            "amount":     abs(float(tx.amount)),
            "created_at": tx.created_at,
            "upi_id":     tx.payu_txn_id or (users[tx.user_id].upi_id if tx.user_id in users else "N/A")
        }
        for tx in pending
    ]


@router.post("/withdrawals/{transaction_id}/approve")
def approve_withdrawal(
    transaction_id: int,
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

    tx.status = "SUCCESS"
    db.add(tx)
    db.commit()

    # NOTIFY USER
    try:
        add_user_notification(
            db, tx.user_id,
            "Withdrawal Successful ✅",
            f"Your withdrawal request of ₹{abs(float(tx.amount))} has been approved and sent to your UPI ID. Check your bank account.",
            "WALLET"
        )
    except Exception: pass

    logger.info(f"Withdrawal {transaction_id} approved by admin={current_user.username}")
    return {"message": "Withdrawal approved"}


@router.post("/withdrawals/{transaction_id}/reject")
def reject_withdrawal(
    transaction_id: int,
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

    tx.status = "FAILED"

    refunded = _refund_withdrawal_if_needed(
        db,
        tx,
        current_user.username,
        "REJECTED_BY_ADMIN",
    )

    db.add(tx)
    db.commit()

    # NOTIFY USER
    try:
        add_user_notification(
            db, tx.user_id,
            "Withdrawal Rejected ❌",
            f"Your withdrawal of ₹{abs(float(tx.amount))} has been rejected. The amount has been refunded to your wallet balance.",
            "WALLET"
        )
    except Exception: pass

    logger.info(
        f"Withdrawal {transaction_id} rejected by admin={current_user.username}; "
        f"refund={float(refunded):.2f}"
    )
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
    ).all()

    user_ids = [p.user_id for p in participants]
    # FIXED: Bulk fetch users
    user_map = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    return [
        {
            "id":            p.user_id,
            "username":      user_map[p.user_id].username     if p.user_id in user_map else "Unknown",
            "avatar_url":    user_map[p.user_id].profile_pic  if p.user_id in user_map else None,
            "game_username": p.game_username,
            "game_uid":      p.game_uid,
            "bgmi_id":       user_map[p.user_id].bgmi_id      if p.user_id in user_map else None,
            "freefire_id":   user_map[p.user_id].freefire_id  if p.user_id in user_map else None,
            "valorant_id":   user_map[p.user_id].valorant_id  if p.user_id in user_map else None,
        }
        for p in participants
    ]


# ─────────────────────────────────────────────────────────────────
# User management
# ─────────────────────────────────────────────────────────────────

@router.get("/users")
def search_users(
    query: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    filters = []
    if query:
        if query.isdigit():
            filters.append(User.id == int(query))
        else:
            filters.append(User.username.ilike(f"%{query}%"))
            filters.append(User.email.ilike(f"%{query}%"))
            filters.append(User.phone_number.ilike(f"%{query}%"))

    if filters:
        users = db.query(User).filter(or_(*filters)).limit(50).all()
    else:
        users = db.query(User).limit(50).all()
    return users


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

    db.add(user)
    db.commit()
    status_str = "Active" if status.is_active else "Banned"
    logger.info(f"User {user_id} set to {status_str} by admin={current_user.username}")
    return {"message": f"User {user.username} is now {status_str}"}


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
        session_ids = [row[0] for row in db.query(ChatSession.id).filter(ChatSession.user_id == user_id).all()]

        deleted_chat_messages = 0
        if session_ids:
            deleted_chat_messages += db.query(ChatMessage).filter(
                ChatMessage.session_id.in_(session_ids)
            ).delete(synchronize_session=False)
        deleted_chat_messages += db.query(ChatMessage).filter(
            ChatMessage.sender_id == user_id
        ).delete(synchronize_session=False)

        deleted_chat_sessions = db.query(ChatSession).filter(
            ChatSession.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_notifications = db.query(Notification).filter(
            Notification.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_participants = db.query(TournamentParticipant).filter(
            TournamentParticipant.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_transactions = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == user_id
        ).delete(synchronize_session=False)

        referred_updates = db.query(User).filter(
            User.referred_by_id == user_id
        ).update({"referred_by_id": None}, synchronize_session=False)

        deleted_username = user.username
        deleted_email = user.email
        db.delete(user)
        db.commit()

        logger.warning(
            f"User deleted by admin={current_user.username}: user_id={user_id}, "
            f"username={deleted_username}, email={deleted_email}, "
            f"deleted_transactions={deleted_transactions}, deleted_participants={deleted_participants}, "
            f"deleted_notifications={deleted_notifications}, deleted_chat_sessions={deleted_chat_sessions}, "
            f"deleted_chat_messages={deleted_chat_messages}, referred_updates={referred_updates}"
        )

        return {
            "message": f"User #{user_id} deleted successfully",
            "deleted_transactions": deleted_transactions,
            "deleted_participants": deleted_participants,
            "deleted_notifications": deleted_notifications,
            "deleted_chat_sessions": deleted_chat_sessions,
            "deleted_chat_messages": deleted_chat_messages,
            "referred_updates": referred_updates,
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

    # FIXED: Convert float to Decimal before arithmetic — wallet_balance is Numeric(12,2)
    decimal_amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    new_balance = (user.wallet_balance or Decimal(0)) + decimal_amount
    if new_balance < Decimal(0):
        raise HTTPException(status_code=400, detail="Adjustment would result in negative balance")

    user.wallet_balance = new_balance
    tx = WalletTransaction(
        user_id=user_id,
        amount=decimal_amount,
        transaction_type="ADMIN_ADJUSTMENT",
        status="SUCCESS",
        reference_id=f"ADJ_{uuid.uuid4().hex[:8].upper()}"
    )
    db.add(tx)
    db.add(user)
    db.commit()

    logger.info(
        f"Admin adjustment: admin={current_user.username} user={user_id} "
        f"amount={decimal_amount} reason={reason[:100]}"
    )
    return {"message": f"Balance updated. New balance: ₹{float(user.wallet_balance):.2f}"}


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


# ─────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────

@router.post("/notifications/send")
def send_push_notification(
    data: NotificationSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    users = db.query(User).filter(User.role == "USER", User.is_active == True).all()

    for user in users:
        notif = Notification(
            user_id=user.id,
            title=data.title,
            content=data.body,
            type="SYSTEM"
        )
        db.add(notif)

    db.commit()
    logger.info(
        f"Broadcast sent to {len(users)} users by admin={current_user.username}: '{data.title}'"
    )
    return {"message": f"Broadcast '{data.title}' sent to {len(users)} users"}


@router.post("/developer/notifications/send")
def send_developer_push_notification(
    data: NotificationSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_otp),
):
    return send_push_notification(data=data, db=db, current_user=current_user)


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
    q = db.query(WalletTransaction).order_by(WalletTransaction.created_at.desc())

    if status:
        q = q.filter(WalletTransaction.status == status.upper())
    if type:
        q = q.filter(WalletTransaction.transaction_type == type.upper())

    txs = q.limit(limit * 3).all()

    # FIXED: Bulk-load all needed users in one query (eliminates N+1)
    user_ids = list({tx.user_id for tx in txs})
    users = {}
    if user_ids:
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    res = []
    for tx in txs:
        u        = users.get(tx.user_id)
        username = u.username if u else "Unknown"
        email    = u.email    if u else ""
        phone    = u.phone_number if u else None
        upi_id   = u.upi_id if u else None

        if search:
            search_lower = search.lower()
            if not any([
                search_lower in username.lower(),
                search_lower in email.lower(),
                search_lower in (phone or "").lower(),
                search_lower in (upi_id or "").lower(),
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
            "bgmi_id":        u.bgmi_id if u else None,
            "freefire_id":    u.freefire_id if u else None,
            "valorant_id":    u.valorant_id if u else None,
            "user_created_at": u.created_at if u else None,
            "user_updated_at": u.updated_at if u else None,
            "amount":         float(tx.amount),
            "type":           tx.transaction_type,
            "status":         tx.status,
            "reference_id":   tx.reference_id,
            "payu_txn_id":    getattr(tx, 'payu_txn_id', None),
            "gateway_utr":    getattr(tx, 'payu_txn_id', None) if tx.transaction_type == "ADD_MONEY" else None,
            "payment_mode":   getattr(tx, 'payment_mode', None),
            "failure_reason": getattr(tx, 'failure_reason', None),
            "gateway_order_id": getattr(tx, 'gateway_order_id', None),
            "gateway_payment_id": getattr(tx, 'gateway_payment_id', None),
            "gateway_signature": getattr(tx, 'gateway_signature', None),
            "withdrawal_upi_id": getattr(tx, 'payu_txn_id', None) if tx.transaction_type == "WITHDRAWAL" else None,
            "created_at":     tx.created_at,
            "updated_at":     tx.updated_at,
        })
        if len(res) >= limit:
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

    user.wallet_balance = (user.wallet_balance or Decimal("0.00")) + credit_amount
    tx.status = "SUCCESS"
    tx.payment_mode = tx.payment_mode or "MANUAL_APPROVE"
    tx.failure_reason = None

    db.add(tx)
    db.add(user)
    db.commit()

    try:
        add_user_notification(
            db, tx.user_id,
            "Payment Confirmed ✅",
            f"₹{float(credit_amount):.0f} has been added to your ZexPlay wallet.",
            "WALLET"
        )
    except Exception:
        pass

    logger.warning(
        f"Manual credit approved by admin={current_user.username} for tx={transaction_id} "
        f"user={tx.user_id} amount={float(credit_amount):.2f}"
    )
    background_tasks.add_task(ws_manager.broadcast_to_admins, {"type": "finance_update"})
    return {"message": f"Transaction #{transaction_id} approved and credited."}


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
    return [
        {"id": u.id, "username": u.username, "email": u.email, "balance": float(u.wallet_balance)}
        for u in users
    ]
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
