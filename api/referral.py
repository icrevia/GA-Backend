from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User
from models.wallet import WalletTransaction
from api.deps import get_current_user
from typing import List, Any
from pydantic import BaseModel

router = APIRouter()

class ReferralStats(BaseModel):
    referral_code: str
    total_referrals: int
    total_earned: float

@router.get("/stats", response_model=ReferralStats)
def get_referral_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Any:
    # Count success referrals (where users were referred by current_user)
    count = db.query(User).filter(User.referred_by_id == current_user.id).count()
    
    # Calculate total earned from REFERRAL_REWARD transactions
    earned = db.query(WalletTransaction).filter(
        WalletTransaction.user_id == current_user.id,
        WalletTransaction.transaction_type == "REFERRAL_REWARD",
        WalletTransaction.status == "SUCCESS"
    ).all()
    
    total_earned = sum(float(t.amount) for t in earned)
    
    return {
        "referral_code": current_user.referral_code or "ZEXPLAY",
        "total_referrals": count,
        "total_earned": total_earned
    }
