from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta, timezone
from api.deps import get_current_user, get_current_user_profile, get_current_active_admin
from core.database import get_db_sync as get_db
from models.user import User
from models.banner import HomeBanner
from schemas.user import UserResponse, UserUpdate, FullProfileResponse
from services.match_stats import compute_match_stats_for_user
from services.restrictions import get_active_restrictions_for_user, serialize_user_restriction
from services.wallet_balances import get_wallet_breakdown
from core.config import settings
from sqlalchemy import func as sqlfunc, case as sqcase, or_, and_, select
from models.wallet import WalletTransaction
from models.tournament import Tournament
from models.participant import TournamentParticipant
from services.match_stats import (
    compute_match_stats_for_user,
    normalize_leaderboard_category,
    leaderboard_prize_payment_mode,
)
import uuid, os, logging, io
from PIL import Image

logger = logging.getLogger("GamerzAdda.users")

PROFILE_PIC_DIR = "static/profile_pics"
PROFILE_PIC_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB (input limit)
PROFILE_PIC_TARGET_KB = 100
ALLOWED_IMG_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

def compress_image(data: bytes, target_kb: int = 100, max_dim: int = 1024) -> bytes:
    """Resizes and compresses an image to stay around target_kb."""
    try:
        img = Image.open(io.BytesIO(data))
        # Resize if too large
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        quality = 90
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        
        # Iteratively reduce quality if still too large
        while output.tell() > target_kb * 1024 and quality > 15:
            quality -= 5
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=quality, optimize=True)
        
        return output.getvalue()
    except Exception as e:
        logger.error(f"Image compression failed: {e}")
        return data # Fallback to original if compression fails


router = APIRouter()


@router.post("/me/profile-pic", response_model=UserResponse)
def upload_profile_pic(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
):
    """Upload / replace the authenticated user's profile picture (max 1 MB, JPEG/PNG/WebP)."""

    # ── Content-type guard ────────────────────────────────────────────────────
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in ALLOWED_IMG_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Only JPEG, PNG, or WebP images are accepted."
        )

    # ── Read & size guard ─────────────────────────────────────────────────────
    data = file.file.read(PROFILE_PIC_MAX_UPLOAD_BYTES + 1)
    if len(data) > PROFILE_PIC_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Image is too large. Maximum allowed upload size is 5 MB."
        )

    # ── Ensure storage directory ──────────────────────────────────────────────
    os.makedirs(PROFILE_PIC_DIR, exist_ok=True)

    # ── Delete previous custom pic (keep static avatar ones) ─────────────────
    old_pic = current_user.profile_pic or ""
    if f"/{PROFILE_PIC_DIR}/" in old_pic:
        try:
            old_filename = old_pic.rsplit("/", 1)[-1]
            old_path = os.path.join(PROFILE_PIC_DIR, old_filename)
            if os.path.isfile(old_path):
                os.remove(old_path)
        except Exception as cleanup_err:
            logger.warning("Could not remove old profile pic: %s", cleanup_err)

    # ── Compress Image ────────────────────────────────────────────────────────
    compressed_data = compress_image(data, target_kb=PROFILE_PIC_TARGET_KB)

    # ── Persist new file ──────────────────────────────────────────────────────
    filename = f"user_{current_user.id}_{uuid.uuid4().hex[:12]}.jpg"
    
    from services.storage import upload_file
    try:
        public_url = upload_file(compressed_data, filename, sub_dir="profile_pics")
    except Exception as e:
        logger.error(f"Failed to upload profile pic to storage: {e}")
        # Final emergency fallback if even service fails
        save_path = os.path.join(PROFILE_PIC_DIR, filename)
        with open(save_path, "wb") as f:
            f.write(compressed_data)
        base_url = (settings.APP_URL or "").rstrip("/")
        public_url = f"{base_url}/static/profile_pics/{filename}"

    current_user.profile_pic = public_url
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    active_restrictions = get_active_restrictions_for_user(db, current_user.id)
    wallet_breakdown = get_wallet_breakdown(current_user)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "wallet_balance": float(wallet_breakdown["balance"]),
        "deposit_balance": float(wallet_breakdown["deposit_balance"]),
        "winning_balance": float(wallet_breakdown["winning_balance"]),
        "bonus_balance": float(wallet_breakdown["bonus_balance"]),
        "profile_pic": current_user.profile_pic,
        "bio": current_user.bio,
        "freefire_id": current_user.freefire_id,
        "is_active": bool(current_user.is_active),
        "face_image_path": getattr(current_user, "face_image_path", None),
        "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
    }


class DeviceTokenRequest(BaseModel):
    fcm_token: str


@router.post("/device-token", status_code=200)
def save_device_token(
    payload: DeviceTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save / refresh the FCM push notification token for this device with redundant write protection."""
    token = payload.fcm_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="fcm_token cannot be empty")
    
    # Optimization: Only write to DB if the token actually changed.
    # This prevents an expensive DB write on every app startup.
    if current_user.fcm_token != token:
        current_user.fcm_token = token
        db.add(current_user)
        db.commit()
    
    return {"message": "Device token saved"}


class TestNotifRequest(BaseModel):
    user_id: int
    title: str = "🔔 Test Notification"
    body: str  = "Notification is working! GamerzAdda ✅"


@router.post("/test-notification", status_code=200)
def send_test_notification(
    payload: TestNotifRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),  # admin only
):
    """Admin-only: Send a test push notification to any user to verify FCM is working."""
    from services.push_notifications import send_push
    target = db.query(User).filter(User.id == payload.user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.fcm_token:
        raise HTTPException(
            status_code=400,
            detail=f"User {payload.user_id} has no FCM token saved. "
                   "Make sure they opened the app after the latest update."
        )
    success = send_push(target.fcm_token, payload.title, payload.body)
    if success:
        return {"message": f"✅ Test notification sent to user {payload.user_id}"}
    raise HTTPException(status_code=500, detail="FCM send failed — check Railway logs and FIREBASE_SERVICE_ACCOUNT_JSON env var")


class BroadcastNotifRequest(BaseModel):
    title: str = "📢 GamerzAdda"
    body: str


@router.post("/broadcast-notification", status_code=200)
def broadcast_notification(
    payload: BroadcastNotifRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin-only: Send a push notification to ALL users with an FCM token."""
    from services.push_notifications import send_push_to_many
    tokens = [
        u.fcm_token for u in
        db.query(User.fcm_token).filter(User.fcm_token.isnot(None)).all()
        if u.fcm_token
    ]
    if not tokens:
        raise HTTPException(status_code=400, detail="No users have FCM tokens yet — make sure app is updated and users have logged in")
    sent = send_push_to_many(tokens, payload.title, payload.body)
    return {"message": f"✅ Sent to {sent}/{len(tokens)} devices"}


def _normalize_phone(phone_number: str) -> str:
    normalized = phone_number.strip().replace(" ", "")
    if len(normalized) == 10 and normalized.isdigit():
        normalized = f"+91{normalized}"
    return normalized


@router.get("/my-fcm-token")
def get_my_fcm_token(
    current_user: User = Depends(get_current_user),
):
    """Returns current user's FCM token — use this to test Firebase Console notifications."""
    if not current_user.fcm_token:
        raise HTTPException(
            status_code=404,
            detail="No FCM token found. Rebuild + reinstall the app with google-services.json, then login again."
        )
    return {"fcm_token": current_user.fcm_token}


@router.get("/me", response_model=UserResponse)
def read_user_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Optimization: Use pre-loaded restrictions from memory (joinedload) instead of a new DB call.
    from services.restrictions import is_restriction_currently_active, serialize_user_restriction
    from datetime import datetime
    now_value = datetime.utcnow()
    
    active_restrictions = [
        r for r in getattr(current_user, "restrictions", [])
        if is_restriction_currently_active(r, now_value)
    ]
    
    wallet_breakdown = get_wallet_breakdown(current_user)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "wallet_balance": float(wallet_breakdown["balance"]),
        "deposit_balance": float(wallet_breakdown["deposit_balance"]),
        "winning_balance": float(wallet_breakdown["winning_balance"]),
        "bonus_balance": float(wallet_breakdown["bonus_balance"]),
        "profile_pic": current_user.profile_pic,
        "bio": current_user.bio,
        "freefire_id": current_user.freefire_id,
        "is_active": bool(current_user.is_active),
        "face_image_path": getattr(current_user, "face_image_path", None),
        "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
    }


@router.get("/me/stats")
def read_user_me_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
):
    return compute_match_stats_for_user(db, current_user.id)


@router.get("/profile-full", response_model=FullProfileResponse)
def read_user_profile_full(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
):
    from services.restrictions import is_restriction_currently_active
    from datetime import datetime
    now_value = datetime.utcnow()
    
    active_restrictions = [
        r for r in getattr(current_user, "restrictions", [])
        if is_restriction_currently_active(r, now_value)
    ]
    
    wallet_breakdown = get_wallet_breakdown(current_user)
    stats_data = compute_match_stats_for_user(db, current_user.id)
    
    return {
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "phone_number": current_user.phone_number,
            "role": current_user.role,
            "wallet_balance": float(wallet_breakdown["balance"]),
            "deposit_balance": float(wallet_breakdown["deposit_balance"]),
            "winning_balance": float(wallet_breakdown["winning_balance"]),
            "bonus_balance": float(wallet_breakdown["bonus_balance"]),
            "profile_pic": current_user.profile_pic,
            "bio": current_user.bio,
            "freefire_id": current_user.freefire_id,
            "is_active": bool(current_user.is_active),
            "face_image_path": getattr(current_user, "face_image_path", None),
            "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
        },
        "stats": stats_data,
        "balance_details": {
            "total": float(wallet_breakdown["balance"]),
            "deposit": float(wallet_breakdown["deposit_balance"]),
            "winning": float(wallet_breakdown["winning_balance"]),
            "bonus": float(wallet_breakdown["bonus_balance"]),
        }
    }


@router.put("/me", response_model=UserResponse)
def update_user_me(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile)
):
    if user_update.username is not None:
        new_username = user_update.username.strip()
        if new_username and new_username != current_user.username:
            # Check if username is already taken by someone else
            existing = db.query(User).filter(User.username == new_username).first()
            if existing:
                raise HTTPException(status_code=400, detail="Username already taken")
            current_user.username = new_username

    if user_update.bio is not None:
        cleaned_bio = user_update.bio.strip()
        current_user.bio = cleaned_bio or None
    if user_update.freefire_id is not None:
        current_user.freefire_id = user_update.freefire_id

    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    active_restrictions = get_active_restrictions_for_user(db, current_user.id)
    wallet_breakdown = get_wallet_breakdown(current_user)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "role": current_user.role,
        "wallet_balance": float(wallet_breakdown["balance"]),
        "deposit_balance": float(wallet_breakdown["deposit_balance"]),
        "winning_balance": float(wallet_breakdown["winning_balance"]),
        "bonus_balance": float(wallet_breakdown["bonus_balance"]),
        "profile_pic": current_user.profile_pic,
        "bio": current_user.bio,
        "freefire_id": current_user.freefire_id,
        "is_active": bool(current_user.is_active),
        "face_image_path": getattr(current_user, "face_image_path", None),
        "active_restrictions": [serialize_user_restriction(r) for r in active_restrictions],
    }


@router.get("/", response_model=List[UserResponse])
def read_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
    skip: int = 0,
    limit: int = 100
):
    users = db.query(User).offset(skip).limit(limit).all()
    return users


# ─────────────────────────────────────────────────────────────────
# Public Leaderboard — game/time aware
# ─────────────────────────────────────────────────────────────────

_LEADERBOARD_TIME_RANGES = {
    "today": "today",
    "last_7_days": "last_7_days",
    "last7": "last_7_days",
    "7d": "last_7_days",
    "last_30_days": "last_30_days",
    "last30": "last_30_days",
    "30d": "last_30_days",
    "lifetime": "lifetime",
    "all_time": "lifetime",
    "all": "lifetime",
}


def _normalize_leaderboard_time_range(raw: str | None) -> str | None:
    if not raw:
        return None
    clean = "_".join(raw.strip().lower().replace("-", "_").split())
    return _LEADERBOARD_TIME_RANGES.get(clean)


def _leaderboard_range_start(now_utc: datetime, time_range: str) -> datetime | None:
    if time_range == "lifetime":
        return None
    if time_range == "today":
        return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "last_7_days":
        return now_utc - timedelta(days=7)
    if time_range == "last_30_days":
        return now_utc - timedelta(days=30)
    return None


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

@router.get("/leaderboard")
def get_leaderboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
    category: str = Query(default="free_fire"),
    time_range: str = Query(default="lifetime"),
    limit: int = Query(default=50, ge=1, le=100),
):

    normalized_category = normalize_leaderboard_category(category)
    if not normalized_category:
        raise HTTPException(
            status_code=400,
            detail="Invalid category. Use one of: free_fire, free_fire_max, clash_squad",
        )

    normalized_time_range = _normalize_leaderboard_time_range(time_range)
    if not normalized_time_range:
        raise HTTPException(
            status_code=400,
            detail="Invalid time_range. Use one of: today, last_7_days, last_30_days, lifetime",
        )

    now_utc = datetime.now(timezone.utc)
    range_start = _leaderboard_range_start(now_utc, normalized_time_range)

    # Convert classification to SQL-friendly patterns
    game_patterns = {
        "free_fire_max": ["%free fire max%", "%free fire%max%", "%max%free fire%"],
        "clash_squad": ["%clash squad%", "%clash%"],
        "fan_battle": ["%fan battle%", "%fanbattle%", "%fan%battle%"],
        "free_fire": ["%free fire%", "%freefire%"],
    }
    
    selected_patterns = game_patterns.get(normalized_category, ["%"])
    game_filter = or_(*[Tournament.game_name.ilike(p) for p in selected_patterns])

    # We need to filter tournaments by status and category
    tournament_subq = (
        db.query(Tournament.id)
        .filter(Tournament.status == "COMPLETED", game_filter)
    )
    if range_start:
        tournament_subq = tournament_subq.filter(
            or_(
                Tournament.updated_at >= range_start,
                Tournament.match_time >= range_start
            )
        )
    tournament_ids_subq = tournament_subq.subquery()

    # Query stats
    stats_query = (
        db.query(
            TournamentParticipant.user_id,
            sqlfunc.count(TournamentParticipant.id).label("matches"),
            sqlfunc.sum(
                sqcase(
                    (Tournament.winner_id == TournamentParticipant.user_id, 1),
                    else_=0,
                )
            ).label("wins"),
        )
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .filter(TournamentParticipant.tournament_id.in_(select(tournament_ids_subq.c.id)))
        .group_by(TournamentParticipant.user_id)
        .subquery()
    )

    # Query earnings
    prize_payment_mode = leaderboard_prize_payment_mode(normalized_category)
    earnings_query = (
        db.query(
            WalletTransaction.user_id,
            sqlfunc.sum(WalletTransaction.amount).label("earnings")
        )
        .filter(WalletTransaction.status == "SUCCESS")
    )
    if prize_payment_mode:
        earnings_query = earnings_query.filter(
            or_(
                WalletTransaction.transaction_type == prize_payment_mode,
                and_(
                    WalletTransaction.transaction_type == "PRIZE_WIN",
                    WalletTransaction.payment_mode == prize_payment_mode
                )
            )
        )
    else:
        # Fallback to all REWARD or PRIZE types if no specific mode found
        earnings_query = earnings_query.filter(
            or_(
                WalletTransaction.transaction_type.ilike("%REWARD%"),
                WalletTransaction.transaction_type.ilike("%PRIZE%"),
                WalletTransaction.payment_mode.ilike("%PRIZE%")
            )
        )

    if range_start:
        earnings_query = earnings_query.filter(WalletTransaction.created_at >= range_start)

    earnings_subq = earnings_query.group_by(WalletTransaction.user_id).subquery()

    # Final combined query with sorting and limit
    final_query = (
        db.query(
            User,
            sqlfunc.coalesce(stats_query.c.matches, 0).label("total_matches"),
            sqlfunc.coalesce(stats_query.c.wins, 0).label("total_wins"),
            sqlfunc.coalesce(earnings_subq.c.earnings, 0.0).label("total_earnings")
        )
        .outerjoin(stats_query, User.id == stats_query.c.user_id)
        .outerjoin(earnings_subq, User.id == earnings_subq.c.user_id)
        .filter(User.is_active == True)
        .filter(or_(stats_query.c.user_id.isnot(None), earnings_subq.c.user_id.isnot(None)))
        .order_by(
            sqlfunc.coalesce(earnings_subq.c.earnings, 0.0).desc(),
            sqlfunc.coalesce(stats_query.c.wins, 0).desc(),
            sqlfunc.coalesce(stats_query.c.matches, 0).desc(),
            User.username.asc()
        )
        .limit(limit)
    )

    leaderboard_users = final_query.all()
    
    return [
        {
            "id": row.User.id,
            "username": row.User.username,
            "bio": row.User.bio,
            "profile_pic": row.User.profile_pic,
            "total_matches": row.total_matches,
            "total_wins": row.total_wins,
            "total_earnings": float(row.total_earnings),
        }
        for row in leaderboard_users
    ]


# ─────────────────────────────────────────────────────────────────
# Public Home Banners — active banners for the Android carousel
# ─────────────────────────────────────────────────────────────────

@router.get("/banners")
def get_home_banners(
    page_key: str = Query("HOME"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return currently active, in-schedule banners for a specific page ordered by sort_order.
    Used by the Android app to populate banner carousels on different screens.
    """
    now = datetime.utcnow()

    rows = (
        db.query(HomeBanner)
        .filter(HomeBanner.is_active == True, HomeBanner.page_key == page_key)
        .order_by(HomeBanner.sort_order.asc(), HomeBanner.created_at.desc())
        .all()
    )

    result = []
    for banner in rows:
        # Skip banners that haven't started yet
        if banner.starts_at:
            starts_at = banner.starts_at
            if getattr(starts_at, "tzinfo", None) is not None:
                starts_at = starts_at.replace(tzinfo=None)
            if starts_at > now:
                continue

        # Skip banners that have expired
        if banner.ends_at:
            ends_at = banner.ends_at
            if getattr(ends_at, "tzinfo", None) is not None:
                ends_at = ends_at.replace(tzinfo=None)
            if ends_at <= now:
                continue

        result.append({
            "id": banner.id,
            "title": banner.title,
            "image_url": banner.image_url,
            "redirect_url": banner.redirect_url,
            "sort_order": int(banner.sort_order or 0),
        })

    return result
