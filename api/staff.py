"""
Staff Panel API
---------------
Staff members are ADMIN users with admin_permissions containing STAFF:FF, STAFF:MAX, and/or STAFF:CS.
This router handles all operations a staff member needs to manage their assigned tournaments.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional, Any
from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
import uuid

from core.database import get_db_sync as get_db
from core.security import decode_access_token
from models.user import User
from models.tournament import Tournament
from models.participant import TournamentParticipant
from models.notification import Notification
from models.wallet import WalletTransaction
from schemas.admin import TournamentConclude
from services.wallet_balances import WALLET_BUCKET_WINNING, credit_wallet, to_money
from services.match_stats import classify_game_mode, leaderboard_prize_payment_mode

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# ─────────────────────────────────────────────────────────────────
# Game name → permission key mapping
# ─────────────────────────────────────────────────────────────────
GAME_PERMISSION_MAP = {
    "free fire": "STAFF:FF",
    "freefire": "STAFF:FF",
    "ff": "STAFF:FF",
    "max": "STAFF:MAX",
    "free fire max": "STAFF:MAX",
    "freefire max": "STAFF:MAX",
    "cs": "STAFF:CS",
    "csgo": "STAFF:CS",
    "cs go": "STAFF:CS",
    "cs2": "STAFF:CS",
    "bgmi": "STAFF:CS",     # CS slot used for BGMI/other if no specific slot
    "clash squad": "STAFF:CS",
    "clashsquad": "STAFF:CS",
    "clash_squad": "STAFF:CS",
}

STAFF_PERMISSION_KEYS = {"STAFF:FF", "STAFF:MAX", "STAFF:CS"}

PERMISSION_LABEL_MAP = {
    "STAFF:FF": "Free Fire",
    "STAFF:MAX": "Free Fire MAX",
    "STAFF:CS": "CS",
}


def _get_staff_permissions(user: User) -> set[str]:
    """Return the set of STAFF:* permissions this user has."""
    raw = (user.admin_permissions or "")
    parts = {p.strip().upper() for p in raw.split(",") if p.strip()}
    return parts & STAFF_PERMISSION_KEYS


def _game_matches_permission(game_name: str, allowed: set[str]) -> bool:
    key = GAME_PERMISSION_MAP.get(game_name.lower().strip())
    if key:
        return key in allowed
    # Super admin (all permissions) — allow any game
    if allowed == STAFF_PERMISSION_KEYS:
        return True
    return False


def _add_user_notification(db: Session, user_id: int, title: str, body: str, category: str = "APP"):
    try:
        notif = Notification(
            user_id=user_id,
            title=title,
            body=body,
            category=category,
            is_read=False,
        )
        db.add(notif)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# Auth dependency
# ─────────────────────────────────────────────────────────────────
def get_current_staff(
    request: Request,
    db: Session = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required")

    # Check that they have at least one STAFF:* permission
    perms = _get_staff_permissions(user)
    if not perms:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No staff game permissions assigned")

    return user


# ─────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────
class SetRoomRequest(BaseModel):
    room_id: str
    room_password: str


class DeclareWinnerRequest(BaseModel):
    winner_user_id: int


class TournamentOut(BaseModel):
    id: int
    title: str
    game_name: str
    status: str
    match_type: str
    entry_fee: float
    prize_pool: float
    match_time: datetime
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    max_slots: int
    participant_count: int
    map_name: Optional[str] = None
    game_image_url: Optional[str] = None
    per_kill_prize: Optional[float] = 0.0
    prize_distribution: Optional[List[Any]] = None

    class Config:
        from_attributes = True


class ParticipantOut(BaseModel):
    user_id: int
    username: str
    profile_pic: Optional[str] = None
    game_username: Optional[str] = None
    game_uid: Optional[str] = None
    slot_no: Optional[int] = None
    kills: Optional[int] = None
    participant_rank: Optional[int] = None

    class Config:
        from_attributes = True


class StaffMeOut(BaseModel):
    id: int
    username: str
    email: str
    profile_pic: Optional[str] = None
    allowed_games: List[str]
    allowed_permissions: List[str]


# ─────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────

@router.get("/me", response_model=StaffMeOut)
def get_staff_me(current_user: User = Depends(get_current_staff)):
    perms = _get_staff_permissions(current_user)
    allowed_games = [PERMISSION_LABEL_MAP[p] for p in sorted(perms)]
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "profile_pic": current_user.profile_pic,
        "allowed_games": allowed_games,
        "allowed_permissions": list(perms),
    }


@router.get("/tournaments", response_model=List[TournamentOut])
def list_staff_tournaments(
    search: Optional[str] = None,
    game: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    perms = _get_staff_permissions(current_user)
    query = db.query(Tournament)

    # If searching by ID
    if search and search.strip().isdigit():
        query = query.filter(Tournament.id == int(search.strip()))
    elif search:
        query = query.filter(Tournament.title.ilike(f"%{search.strip()}%"))

    if status_filter:
        query = query.filter(Tournament.status == status_filter.upper())

    tournaments = query.order_by(Tournament.match_time.desc()).limit(100).all()

    # Filter to only games this staff member can manage
    filtered = []
    for t in tournaments:
        if _game_matches_permission(t.game_name, perms):
            count = db.query(TournamentParticipant).filter(
                TournamentParticipant.tournament_id == t.id
            ).count()
            filtered.append({
                "id": t.id,
                "title": t.title,
                "game_name": t.game_name,
                "status": t.status,
                "match_type": t.match_type,
                "entry_fee": float(t.entry_fee),
                "prize_pool": float(t.prize_pool),
                "match_time": t.match_time,
                "room_id": t.room_id,
                "room_password": t.room_password,
                "max_slots": t.max_slots,
                "participant_count": count,
                "map_name": t.map_name,
                "game_image_url": t.game_image_url,
                "per_kill_prize": float(t.per_kill_prize or 0.0),
                "prize_distribution": t.prize_distribution,
            })

    return filtered


@router.get("/tournaments/{tournament_id}", response_model=TournamentOut)
def get_staff_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    perms = _get_staff_permissions(current_user)
    t = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if not _game_matches_permission(t.game_name, perms):
        raise HTTPException(status_code=403, detail="You are not assigned to this game type")

    count = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == t.id
    ).count()

    return {
        "id": t.id, "title": t.title, "game_name": t.game_name, "status": t.status,
        "match_type": t.match_type, "entry_fee": float(t.entry_fee), "prize_pool": float(t.prize_pool),
        "match_time": t.match_time, "room_id": t.room_id, "room_password": t.room_password,
        "max_slots": t.max_slots, "participant_count": count, "map_name": t.map_name,
        "game_image_url": t.game_image_url,
        "per_kill_prize": float(t.per_kill_prize or 0.0),
        "prize_distribution": t.prize_distribution,
    }


@router.get("/tournaments/{tournament_id}/roster")
def get_staff_roster(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    perms = _get_staff_permissions(current_user)
    t = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if not _game_matches_permission(t.game_name, perms):
        raise HTTPException(status_code=403, detail="You are not assigned to this game type")

    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).order_by(TournamentParticipant.slot_no.asc().nulls_last()).all()

    user_ids = [p.user_id for p in participants]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    result = []
    for p in participants:
        u = users.get(p.user_id)
        team_members = p.team_members or []
        primary = team_members[0] if team_members else None
        result.append({
            "user_id": p.user_id,
            "username": u.username if u else "Unknown",
            "profile_pic": u.profile_pic if u else None,
            "game_username": primary["name"] if primary else p.game_username,
            "game_uid": primary["uid"] if primary else p.game_uid,
            "slot_no": p.slot_no,
            "kills": p.kills,
            "participant_rank": p.participant_rank,
        })
    return result


@router.post("/tournaments/{tournament_id}/set-room")
def staff_set_room(
    tournament_id: int,
    data: SetRoomRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    perms = _get_staff_permissions(current_user)
    t = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if not _game_matches_permission(t.game_name, perms):
        raise HTTPException(status_code=403, detail="You are not assigned to this game type")
    if t.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")

    t.room_id = data.room_id.strip()
    t.room_password = data.room_password.strip()
    t.status = "LIVE"
    db.add(t)
    db.commit()

    # Notify all participants
    try:
        parts = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id
        ).all()
        for p in parts:
            _add_user_notification(
                db, p.user_id,
                "MATCH IS LIVE! 🚀",
                f"Room ID: {data.room_id} | Pass: {data.room_password} for '{t.title}'. Join quickly!",
                "APP"
            )
        db.commit()
    except Exception:
        pass

    return {"message": "Room keys set and match is now LIVE", "room_id": t.room_id}


@router.post("/tournaments/{tournament_id}/declare-winner")
def staff_declare_winner(
    tournament_id: int,
    data: DeclareWinnerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    perms = _get_staff_permissions(current_user)
    t = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if not _game_matches_permission(t.game_name, perms):
        raise HTTPException(status_code=403, detail="You are not assigned to this game type")
    if t.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")

    # Verify winner is actually a participant
    participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == data.winner_user_id,
    ).first()
    if not participant:
        raise HTTPException(status_code=400, detail="Winner must be a registered participant")

    winner = db.query(User).filter(User.id == data.winner_user_id).first()
    if not winner:
        raise HTTPException(status_code=404, detail="Winner user not found")

    t.winner_id = data.winner_user_id
    t.status = "COMPLETED"
    db.add(t)
    db.commit()

    # Notify winner
    try:
        _add_user_notification(
            db, winner.id,
            "You Won! 🏆",
            f"Congratulations! You've been declared the winner of '{t.title}'!",
            "APP"
        )
        db.commit()
    except Exception:
        pass

    return {
        "message": f"Winner declared: {winner.username}",
        "winner_id": winner.id,
        "winner_username": winner.username,
    }


@router.post("/tournaments/{tournament_id}/conclude")
def staff_conclude_tournament(
    tournament_id: int,
    data: TournamentConclude,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff)
):
    perms = _get_staff_permissions(current_user)
    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if not _game_matches_permission(tournament.game_name, perms):
        raise HTTPException(status_code=403, detail="You are not assigned to this game type")
    if tournament.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")

    per_kill_prize = to_money(getattr(tournament, 'per_kill_prize', 0.0))
    leaderboard_category = classify_game_mode(getattr(tournament, "game_name", None))
    payout_payment_mode = leaderboard_prize_payment_mode(leaderboard_category)

    total_paid = Decimal("0.00")
    winners_set = set()

    # ─── PROCESS MANUAL PRIZES ────────────────────────────────────────
    if data.manual_prizes:
        for entry in data.manual_prizes:
            user_id = entry.user_id
            amount = to_money(entry.amount)

            # Update stats
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
                _add_user_notification(
                    db, member_user.id,
                    "TOURNAMENT WINNINGS! 🏆",
                    f"Congratulations! You've been awarded ₹{amount:.2f} for '{tournament.title}'. Check your wallet!",
                    "APP"
                )
    else:
        # Fallback reward logic
        for entry in data.kill_rewards:
            user_id = entry.user_id
            kills = entry.kills or 0
                
            participant = db.query(TournamentParticipant).filter(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.user_id == user_id
            ).first()
            if not participant: continue

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
                    reference_id=f"KLL-{tournament_id}-{uuid.uuid4().hex[:6].upper()}",
                    payment_mode=payout_payment_mode,
                    remark=tournament.title
                )
                db.add(tx)
                total_paid += member_prize
                winners_set.add(user_id)
                _add_user_notification(
                    db, member_user.id,
                    "KILL REWARDS! 🎯",
                    f"You've been credited ₹{member_prize:.2f} for {kills} kills in '{tournament.title}'!",
                    "APP"
                )

    if data.winner_id:
        tournament.winner_id = int(data.winner_id)
    tournament.status = "COMPLETED"
    db.add(tournament)
    db.commit()

    return {
        "status": "concluded",
        "total_prizes_distributed": float(total_paid),
        "winners_count": len(winners_set)
    }
