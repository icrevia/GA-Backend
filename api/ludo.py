from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from core.database import get_db
from models.user import User
from models.ludo import LudoMatch, LudoParticipant
from schemas.ludo import LudoMatchResponse
from api.deps import get_current_user

router = APIRouter()

@router.get("/history", response_model=List[LudoMatchResponse])
def get_ludo_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the Ludo match history for the current user."""
    matches = (
        db.query(LudoMatch)
        .join(LudoParticipant)
        .filter(LudoParticipant.user_id == current_user.id)
        .order_by(LudoMatch.created_at.desc())
        .limit(50)
        .all()
    )
    return matches

@router.get("/live", response_model=List[LudoMatchResponse])
def get_live_ludo_matches(
    db: Session = Depends(get_db)
):
    """Get currently live Ludo matches (for spectator or tracking)."""
    matches = (
        db.query(LudoMatch)
        .filter(LudoMatch.status == "PLAYING")
        .order_by(LudoMatch.created_at.desc())
        .limit(20)
        .all()
    )
    return matches
