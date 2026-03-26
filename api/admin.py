from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Dict
import uuid
import os
import shutil
import logging

from api.deps import get_db, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.config import SystemConfig
from models.notification import Notification
from models.participant import TournamentParticipant
from services.notifications import add_user_notification
from core.websockets import manager as ws_manager

from schemas.admin import (
    SystemConfigUpdate,
    NotificationSendRequest,
    UserStatusUpdate,
    TournamentRoomUpdate,
    TournamentConclude,
    TournamentCreateAdmin
)
from schemas.tournament import TournamentCreate, TournamentResponse

logger = logging.getLogger("zexplay.admin")
router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# APK Upload — FIXED: path traversal prevention + size cap
# ─────────────────────────────────────────────────────────────────

MAX_APK_SIZE_MB = 150
MAX_APK_SIZE_BYTES = MAX_APK_SIZE_MB * 1024 * 1024

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

    total_joins = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0

    total_revenue_pool = abs(float(total_joins))

    total_prizes = db.query(func.sum(Tournament.prize_pool)).filter(
        Tournament.status == "COMPLETED"
    ).scalar() or 0.0

    estimated_revenue = total_revenue_pool - float(total_prizes)

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "total_revenue_pool": total_revenue_pool,
        "total_prizes_distributed": float(total_prizes),
        "estimated_platform_revenue": estimated_revenue
    }


# ─────────────────────────────────────────────────────────────────
# Withdrawal management
# ─────────────────────────────────────────────────────────────────

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
            "upi_id":     users[tx.user_id].upi_id if tx.user_id in users else "N/A"
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

    # Refund the user
    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    user.wallet_balance -= float(tx.amount)  # tx.amount is negative for withdrawals
    db.add(tx)
    db.add(user)
    db.commit()

    logger.info(f"Withdrawal {transaction_id} rejected by admin={current_user.username}")
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
    db.add(user)
    db.commit()
    status_str = "Active" if status.is_active else "Banned"
    logger.info(f"User {user_id} set to {status_str} by admin={current_user.username}")
    return {"message": f"User {user.username} is now {status_str}"}


@router.post("/users/{user_id}/adjust-funds")
def adjust_user_funds(
    user_id: int,
    amount: float,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    # Cap single adjustment to prevent accidental or malicious mass crediting
    if abs(amount) > 50_000:
        raise HTTPException(status_code=400, detail="Single adjustment cannot exceed ₹50,000")

    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.wallet_balance += amount
    tx = WalletTransaction(
        user_id=user_id,
        amount=amount,
        transaction_type="ADMIN_ADJUSTMENT",
        status="SUCCESS",
        reference_id=f"ADJ_{uuid.uuid4().hex[:8].upper()}"
    )
    db.add(tx)
    db.add(user)
    db.commit()

    logger.info(
        f"Admin adjustment: admin={current_user.username} user={user_id} "
        f"amount={amount} reason={reason[:100]}"
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
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    res = []
    for tx in txs:
        u        = users.get(tx.user_id)
        username = u.username if u else "Unknown"
        email    = u.email    if u else ""

        if search:
            search_lower = search.lower()
            if not any([
                search_lower in username.lower(),
                search_lower in email.lower(),
                search_lower in (tx.reference_id or "").lower(),
                search_lower in (tx.payu_txn_id or "").lower(),
                search_lower in str(tx.user_id),
            ]):
                continue

        res.append({
            "id":             tx.id,
            "user_id":        tx.user_id,
            "username":       username,
            "email":          email,
            "amount":         float(tx.amount),
            "type":           tx.transaction_type,
            "status":         tx.status,
            "reference_id":   tx.reference_id,
            "payu_txn_id":    getattr(tx, 'payu_txn_id', None),
            "payment_mode":   getattr(tx, 'payment_mode', None),
            "failure_reason": getattr(tx, 'failure_reason', None),
            "created_at":     tx.created_at,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(
        WalletTransaction.id == transaction_id
    ).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.transaction_type != "ADD_MONEY":
        raise HTTPException(status_code=400, detail="Can only manually credit ADD_MONEY transactions")
    if tx.status == "SUCCESS":
        raise HTTPException(status_code=400, detail="Transaction already credited")
    if tx.status == "FAILED":
        raise HTTPException(status_code=400, detail="Transaction is FAILED. Use adjust-funds instead.")

    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tx.status         = "SUCCESS"
    tx.failure_reason = None
    if not getattr(tx, 'payu_txn_id', None) or not tx.payu_txn_id:
        tx.payu_txn_id = f"ADMIN_CREDITED_BY_{current_user.username}"

    user.wallet_balance += float(tx.amount)
    db.add(tx)
    db.add(user)
    db.commit()

    add_user_notification(
        db, user.id,
        "Payment Manually Credited ✅",
        f"₹{float(tx.amount):.0f} has been manually added to your wallet by support. Sorry for the delay!",
        "WALLET"
    )

    logger.info(f"Manual credit: admin={current_user.username} tx={transaction_id} user={user.id} amount={tx.amount}")
    return {"message": f"Successfully credited ₹{float(tx.amount)} to {user.username}. New balance: ₹{float(user.wallet_balance):.2f}"}


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
    tx.failure_reason = f"MARKED_FAILED_BY_ADMIN:{current_user.username}"
    db.add(tx)
    db.commit()

    logger.info(f"Transaction {transaction_id} marked FAILED by admin={current_user.username}")
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
