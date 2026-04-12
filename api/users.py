from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List
from api.deps import get_current_user, get_current_user_profile, get_current_active_admin
from core.database import get_db_sync as get_db
from models.user import User
from schemas.user import UserResponse, UserUpdate
from services.match_stats import compute_match_stats_for_user
from services.restrictions import get_active_restrictions_for_user, serialize_user_restriction
from services.wallet_balances import get_wallet_breakdown

router = APIRouter()


class DeviceTokenRequest(BaseModel):
    fcm_token: str


@router.post("/device-token", status_code=200)
def save_device_token(
    payload: DeviceTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save / refresh the FCM push notification token for this device."""
    token = payload.fcm_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="fcm_token cannot be empty")
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


@router.get("/me", response_model=UserResponse)
def read_user_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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


@router.get("/me/stats")
def read_user_me_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
):
    return compute_match_stats_for_user(db, current_user.id)


@router.put("/me", response_model=UserResponse)
def update_user_me(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile)
):
    if user_update.username is not None:
        current_user.username = user_update.username

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
# Public Leaderboard — top players by winnings + kills
# ─────────────────────────────────────────────────────────────────

@router.get("/leaderboard")
def get_leaderboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_profile),
    limit: int = 50,
):
    from sqlalchemy import func as sqlfunc, case as sqcase
    from models.participant import TournamentParticipant
    from models.tournament import Tournament
    from services.wallet_balances import get_wallet_breakdown

    # Aggregate stats per user from completed tournaments
    rows = (
        db.query(
            TournamentParticipant.user_id,
            sqlfunc.count(TournamentParticipant.id).label("total_matches"),
            sqlfunc.sum(
                sqcase(
                    (Tournament.winner_id == TournamentParticipant.user_id, 1),
                    else_=0
                )
            ).label("total_wins"),
        )
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .filter(Tournament.status == "COMPLETED")
        .group_by(TournamentParticipant.user_id)
        .all()
    )

    stats_by_user = {
        row.user_id: {
            "total_matches": int(row.total_matches or 0),
            "total_wins": int(row.total_wins or 0),
        }
        for row in rows
    }

    user_ids = list(stats_by_user.keys())
    if not user_ids:
        return []

    users = db.query(User).filter(User.id.in_(user_ids), User.is_active == True).all()

    result = []
    for user in users:
        wb = get_wallet_breakdown(user)
        winning_balance = float(wb.get("winning_balance", 0) or 0)
        stats = stats_by_user.get(user.id, {})
        result.append({
            "id": user.id,
            "username": user.username,
            "bio": user.bio,
            "profile_pic": user.profile_pic,
            "total_matches": stats.get("total_matches", 0),
            "total_wins": stats.get("total_wins", 0),
            "total_earnings": winning_balance,
        })

    # Sort by total earnings desc, then wins desc
    result.sort(key=lambda x: (-x["total_earnings"], -x["total_wins"], -x["total_matches"]))
    return result[:limit]

