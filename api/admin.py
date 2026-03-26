from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Dict
import uuid
import os
import shutil

from api.deps import get_db, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.config import SystemConfig
from models.notification import Notification
from models.participant import TournamentParticipant
from services.notifications import add_user_notification

from schemas.admin import (
    SystemConfigUpdate, 
    NotificationSendRequest, 
    UserStatusUpdate, 
    TournamentRoomUpdate,
    TournamentConclude, 
    TournamentCreateAdmin
)
from schemas.tournament import TournamentCreate, TournamentResponse

router = APIRouter()

@router.post("/config/upload-apk")
def upload_apk(
    file: UploadFile = File(...),
    admin: User = Depends(get_current_active_admin)
):
    """
    Directly upload an APK to the static directory for OTA updates.
    """
    print(f"DEBUG: Received APK upload request for {file.filename} 📥")
    if not file.filename.endswith(".apk"):
        print(f"DEBUG: Invalid file type: {file.filename} ❌")
        raise HTTPException(status_code=400, detail="Only APK files are allowed! 🤖🛡️")
    
    # Save to static directory
    static_dir = "static"
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)
        print(f"DEBUG: Created static directory 📂")
    
    file_path = os.path.join(static_dir, file.filename)
    print(f"DEBUG: Saving file to {file_path} ...")
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        print(f"DEBUG: File saved successfully! ✅")
    except Exception as e:
        print(f"DEBUG: Failed to save file: {str(e)} ❌")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Generate public URL (assuming the API runs on the same domain)
    return {"url": f"/static/{file.filename}", "filename": file.filename}

@router.get("/tournaments", response_model=List[TournamentResponse])
def list_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    return db.query(Tournament).order_by(Tournament.created_at.desc()).all()

@router.post("/tournaments", response_model=TournamentResponse)
def create_tournament(
    data: TournamentCreateAdmin,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    from datetime import datetime
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
        status="UPCOMING"
    )
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

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
        
    db_obj.room_id = data.room_id
    db_obj.room_password = data.room_password
    db_obj.status = "LIVE"
    
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
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tournament not found")
    
    # Cascade delete participants manually to avoid foreign key errors
    db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).delete()
    
    db.delete(tournament)
    db.commit()
    return {"message": "Tournament deleted successfully"}

@router.get("/stats")
def get_admin_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_admin)):
    total_users = db.query(User).count()
    total_tournaments = db.query(Tournament).count()
    
    # Calculate total revenue (commission)
    total_joins = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0
    
    total_revenue_pool = abs(total_joins)
    
    total_prizes = db.query(func.sum(Tournament.prize_pool)).filter(
        Tournament.status == "COMPLETED"
    ).scalar() or 0.0
    
    estimated_revenue = total_revenue_pool - total_prizes

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "total_revenue_pool": total_revenue_pool,
        "total_prizes_distributed": total_prizes,
        "estimated_platform_revenue": estimated_revenue
    }

@router.post("/withdrawals/{transaction_id}/approve")
def approve_withdrawal(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    if tx.transaction_type != "WITHDRAWAL" or tx.status != "PENDING":
        raise HTTPException(status_code=400, detail="Invalid transaction or already processed")
        
    tx.status = "SUCCESS"
    db.add(tx)
    db.commit()
    
    return {"message": "Withdrawal approved"}

@router.post("/withdrawals/{transaction_id}/reject")
def reject_withdrawal(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    if tx.transaction_type != "WITHDRAWAL" or tx.status != "PENDING":
        raise HTTPException(status_code=400, detail="Invalid transaction or already processed")
        
    tx.status = "FAILED"
    
    # Refund the user
    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    user.wallet_balance -= tx.amount
    
    db.add(tx)
    db.add(user)
    db.commit()
    
    return {"message": "Withdrawal rejected and refunded"}

@router.post("/tournaments/{tournament_id}/conclude")
def conclude_tournament(
    tournament_id: int,
    winner_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament already completed")
    
    winner = db.query(User).filter(User.id == winner_id).with_for_update().first()
    if not winner:
        raise HTTPException(status_code=404, detail="Winner user not found")
        
    tournament.winner_id = winner_id
    tournament.status = "COMPLETED"
    winner.wallet_balance += tournament.prize_pool
    
    tx = WalletTransaction(
        user_id=winner_id,
        amount=tournament.prize_pool,
        transaction_type="PRIZE_WIN",
        status="SUCCESS",
        reference_id=f"WIN_TRN_{tournament_id}"
    )
    db.add(tx)
    db.add(winner)
    db.add(tournament)
    db.commit()
    
    return {"message": "Tournament concluded and prize distributed"}

@router.get("/tournaments/{tournament_id}/roster")
def get_tournament_roster(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    participants = db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).all()
    
    user_ids = [p.user_id for p in participants]
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u for u in users}
    
    return [
        {
            "id": p.user_id, 
            "username": user_map[p.user_id].username if p.user_id in user_map else "Unknown",
            "avatar_url": user_map[p.user_id].profile_pic if p.user_id in user_map else None,
            "game_username": p.game_username,
            "game_uid": p.game_uid,
            "bgmi_id": user_map[p.user_id].bgmi_id if p.user_id in user_map else None,
            "freefire_id": user_map[p.user_id].freefire_id if p.user_id in user_map else None,
            "valorant_id": user_map[p.user_id].valorant_id if p.user_id in user_map else None,
        } for p in participants
    ]

@router.post("/tournaments/{tournament_id}/refund")
def refund_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status in ["COMPLETED", "CANCELLED"]:
        raise HTTPException(status_code=400, detail="Cannot refund a completed or cancelled tournament")
        
    joins = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.reference_id.like(f"TRN_{tournament_id}%")
    ).all()
    
    refund_count = 0
    for join_tx in joins:
        user = db.query(User).filter(User.id == join_tx.user_id).with_for_update().first()
        if user:
            user.wallet_balance -= join_tx.amount
            refund_tx = WalletTransaction(
                user_id=user.id,
                amount=-join_tx.amount,
                transaction_type="REFUND",
                status="SUCCESS",
                reference_id=f"REF_TRN_{tournament_id}"
            )
            db.add(refund_tx)
            db.add(user)
            refund_count += 1
            
    tournament.status = "CANCELLED"
    db.add(tournament)
    db.commit()
    return {"message": f"Tournament cancelled. Refunded {refund_count} users."}

@router.get("/withdrawals")
def list_pending_withdrawals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    pending = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "WITHDRAWAL",
        WalletTransaction.status == "PENDING"
    ).all()
    res = []
    for tx in pending:
        u = db.query(User).filter(User.id == tx.user_id).first()
        res.append({
            "id": tx.id,
            "user_id": tx.user_id,
            "username": u.username if u else "Unknown",
            "amount": abs(tx.amount),
            "created_at": tx.created_at,
            "upi_id": u.upi_id if u else "N/A"
        })
    return res

@router.post("/users/{user_id}/adjust-funds")
def adjust_user_funds(
    user_id: int,
    amount: float,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    user.wallet_balance += amount
    tx = WalletTransaction(
        user_id=user_id,
        amount=amount,
        transaction_type="ADMIN_ADJUSTMENT",
        status="SUCCESS",
        reference_id=f"ADJ_{uuid.uuid4().hex[:8].upper()}_{reason[:5]}"
    )
    db.add(tx)
    db.add(user)
    db.commit()
    return {"message": f"Balance updated. New balance: ₹{user.wallet_balance}"}

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
    
    if query:
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
    return {"message": f"User {user.username} is now {status_str}"}

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
    return {"message": f"Config {data.key} updated to {data.value}"}

@router.post("/notifications/send")
def send_push_notification(
    data: NotificationSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    # Save to database for all users so it shows up in their Notification Screen
    users = db.query(User).filter(User.role == "USER").all()
    
    for user in users:
        notif = Notification(
            user_id=user.id,
            title=data.title,
            content=data.body,
            type="SYSTEM"
        )
        db.add(notif)
    
    db.commit()
    return {"message": f"Broadcast '{data.title}' saved for {len(users)} users and sent to topic: {data.topic}"}

@router.get("/transactions")
def list_all_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin),
    status: str = "",      # filter: PENDING / SUCCESS / FAILED
    type: str = "",        # filter: ADD_MONEY / WITHDRAWAL / etc
    search: str = "",      # search: username, email, reference_id, payu_txn_id
    limit: int = 100
):
    """Full transaction audit log — all types, all statuses, all users."""
    q = db.query(WalletTransaction).order_by(WalletTransaction.created_at.desc())
    
    # Apply filters
    if status:
        q = q.filter(WalletTransaction.status == status.upper())
    if type:
        q = q.filter(WalletTransaction.transaction_type == type.upper())

    txs = q.limit(limit * 3).all()  # Over-fetch to allow search filtering
    
    # Build response with user enrichment
    res = []
    for tx in txs:
        u = db.query(User).filter(User.id == tx.user_id).first()
        username = u.username if u else "Unknown"
        email = u.email if u else ""
        
        # Apply search filter
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
            "id": tx.id,
            "user_id": tx.user_id,
            "username": username,
            "email": email,
            "amount": tx.amount,
            "type": tx.transaction_type,
            "status": tx.status,
            "reference_id": tx.reference_id,
            "payu_txn_id": getattr(tx, 'payu_txn_id', None),
            "payment_mode": getattr(tx, 'payment_mode', None),
            "failure_reason": getattr(tx, 'failure_reason', None),
            "created_at": tx.created_at,
        })
        if len(res) >= limit:
            break
    
    return res


@router.get("/finance-stats")
def get_finance_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    """Dashboard stats for admin finance panel."""
    from datetime import datetime, timezone, timedelta
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    total_recharged_today = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.created_at >= today_start
    ).scalar() or 0.0

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

    total_recharged_all = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "ADD_MONEY",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0

    return {
        "total_recharged_today": round(total_recharged_today, 2),
        "failed_today": failed_today,
        "pending_payments": pending_payments,
        "pending_withdrawals": pending_withdrawals,
        "total_recharged_all_time": round(total_recharged_all, 2),
    }


@router.post("/transactions/{transaction_id}/manual-credit")
def manual_credit_transaction(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    """
    Admin manually credits a PENDING ADD_MONEY transaction.
    Used when user paid but webhook/SURL failed to fire.
    Creates full audit trail.
    """
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.transaction_type != "ADD_MONEY":
        raise HTTPException(status_code=400, detail="Can only manually credit ADD_MONEY transactions")
    if tx.status == "SUCCESS":
        raise HTTPException(status_code=400, detail="Transaction already credited")
    if tx.status == "FAILED":
        raise HTTPException(status_code=400, detail="Transaction is marked FAILED. Use adjust-funds instead if needed.")
    
    user = db.query(User).filter(User.id == tx.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    tx.status = "SUCCESS"
    tx.failure_reason = None
    # Mark it as admin-credited so it's distinguishable in the log
    if not getattr(tx, 'payu_txn_id', None) or not tx.payu_txn_id:
        tx.payu_txn_id = f"ADMIN_CREDITED_BY_{current_user.username}"
    
    user.wallet_balance += tx.amount
    db.add(tx)
    db.add(user)
    db.commit()
    
    add_user_notification(
        db, user.id,
        "Payment Manually Credited ✅",
        f"₹{tx.amount:.0f} has been manually added to your wallet by support. Sorry for the delay!",
        "WALLET"
    )
    
    return {
        "message": f"Successfully credited ₹{tx.amount} to {user.username}. New balance: ₹{user.wallet_balance:.2f}"
    }


@router.post("/transactions/{transaction_id}/mark-failed")
def mark_transaction_failed(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    """
    Admin marks a stuck PENDING transaction as FAILED.
    Used when user clearly cancelled/abandoned payment.
    """
    tx = db.query(WalletTransaction).filter(WalletTransaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Transaction is already {tx.status}")
    if tx.transaction_type not in ("ADD_MONEY", "WITHDRAWAL"):
        raise HTTPException(status_code=400, detail="Only ADD_MONEY or WITHDRAWAL can be marked failed")
    
    tx.status = "FAILED"
    tx.failure_reason = f"MARKED_FAILED_BY_ADMIN:{current_user.username}"
    db.add(tx)
    db.commit()
    
    return {"message": f"Transaction #{transaction_id} marked as FAILED."}


@router.get("/leaderboard")
def get_leaderboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    users = db.query(User).filter(User.role == 'USER').order_by(User.wallet_balance.desc()).limit(50).all()
    return [{"id": u.id, "username": u.username, "balance": u.wallet_balance, "is_active": u.is_active} for u in users]

@router.get("/banned_users")
def get_banned_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    users = db.query(User).filter(User.is_active == False).all()
    return [{"id": u.id, "username": u.username, "email": u.email, "balance": u.wallet_balance} for u in users]

@router.post("/tournaments/finish/{tournament_id}", response_model=dict)
def conclude_tournament(
    tournament_id: int,
    data: dict, # Using raw dict to bypass strict schema for now
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    winner_id = data.get("winner_id")
    if not winner_id:
        raise HTTPException(status_code=422, detail="Winner ID is required")
        
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    winner = db.query(User).filter(User.id == winner_id).first()
    if not winner:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Winner user not found")
        
    # Prize Payout
    prize = tournament.prize_pool
    winner.wallet_balance += prize
    
    # Tournament Log
    win_tx = WalletTransaction(
        user_id=winner.id,
        amount=prize,
        transaction_type="TOURNAMENT_WIN",
        status="SUCCESS",
        reference_id=f"WIN_{tournament_id}"
    )
    db.add(win_tx)
    db.add(winner)
    
    tournament.status = "COMPLETED"
    db.add(tournament)
    db.commit()
    
    # Broadcast notification to winner
    try:
        from services.notifications import add_user_notification
        add_user_notification(
            db, 
            winner.id, 
            "CHAMPION! 🏆", 
            f"You won ₹{prize} in {tournament.title}! Check your wallet."
        )
    except: pass
    
    return {"message": f"Tournament concluded. Winner paid ₹{prize}"}

@router.post("/tournaments/{tournament_id}/refund")
def refund_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    participants = db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).all()
    
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
    
    return {"message": f"Refunded all {ref_count} participants"}
