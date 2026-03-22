from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Dict

from api.deps import get_db, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.wallet import WalletTransaction

router = APIRouter()

@router.get("/stats")
def get_admin_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_active_admin)):
    total_users = db.query(User).count()
    total_tournaments = db.query(Tournament).count()
    
    # Calculate total revenue (commission)
    # Commission is usually deduced from the prize pool vs entry fee * participants, or explicitly saved
    # For simplicity, we just calculate successful joins and their total value
    # Let's get total amount of successful JOIN_TOURNAMENT (which is negative in DB)
    total_joins = db.query(func.sum(WalletTransaction.amount)).filter(
        WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
        WalletTransaction.status == "SUCCESS"
    ).scalar() or 0.0
    
    # Value is negative, convert to positive
    total_revenue_pool = abs(total_joins)
    
    total_prizes = db.query(func.sum(Tournament.prize_pool)).filter(
        Tournament.status == "COMPLETED"
    ).scalar() or 0.0
    
    # Approximate commission = entry fees collected - prizes given
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
    # tx.amount is negative, so subtracting it adds the balance back (e.g. - -500 = +500)
    user.wallet_balance -= tx.amount
    
    db.add(tx)
    db.add(user)
    db.commit()
    
    return {"message": "Withdrawal rejected and refunded"}
