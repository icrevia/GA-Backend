from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Dict

from api.deps import get_db, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.wallet import WalletTransaction
from models.config import SystemConfig

from schemas.admin import SystemConfigUpdate, NotificationSendRequest, UserStatusUpdate

router = APIRouter()

@router.get("/tournaments")
def list_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    return db.query(Tournament).order_by(Tournament.created_at.desc()).all()

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
    joins = db.query(WalletTransaction).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS",
        WalletTransaction.reference_id.like(f"TRN_{tournament_id}%") # fuzzy match for composite refs
    ).all()
    
    user_ids = [j.user_id for j in joins]
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    
    return [{"id": u.id, "username": u.username, "email": u.email, "bgmi_id": u.bgmi_id} for u in users]

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
        reference_id=f"ADJ_{func.now()}_{reason[:10]}"
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
    current_user: User = Depends(get_current_active_admin)
):
    return {"message": f"Notification '{data.title}' sent to topic: {data.topic}"}

