from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List

from api.deps import get_db, get_current_user, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.participant import TournamentParticipant
from models.wallet import WalletTransaction
from schemas.tournament import TournamentCreate, TournamentUpdate, TournamentResponse, TournamentJoinResponse

router = APIRouter()

@router.get("/", response_model=List[TournamentResponse])
def get_upcoming_tournaments(db: Session = Depends(get_db)):
    return db.query(Tournament).filter(or_(Tournament.status == "UPCOMING", Tournament.status == "LIVE")).order_by(Tournament.match_time.asc()).all()

@router.post("/", response_model=TournamentResponse)
def create_tournament(
    tournament_in: TournamentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = Tournament(**tournament_in.dict())
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj

@router.put("/{tournament_id}", response_model=TournamentResponse)
def update_tournament(
    tournament_id: int,
    tournament_in: TournamentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    db_obj = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not db_obj:
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    update_data = tournament_in.dict(exclude_unset=True)
    for field in update_data:
        setattr(db_obj, field, update_data[field])
        
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    
    # Broadcast realtime update logic here ideally (call WebSocket manager)
    return db_obj

@router.post("/{tournament_id}/join", response_model=TournamentJoinResponse)
def join_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not db_tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    if db_tournament.status != "UPCOMING":
        raise HTTPException(status_code=400, detail="Cannot join active or completed tournament")
        
    # Check if already joined
    participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id
    ).first()
    
    if participant:
        raise HTTPException(status_code=400, detail="Already joined this tournament")
        
    # Atomic wallet deduction
    # Lock the user row for update
    user_to_update = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    
    if user_to_update.wallet_balance < db_tournament.entry_fee:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")
        
    # Deduct balance
    user_to_update.wallet_balance -= db_tournament.entry_fee
    
    # Create transaction
    transaction = WalletTransaction(
        user_id=user_to_update.id,
        amount=-db_tournament.entry_fee,
        transaction_type="JOIN_TOURNAMENT",
        status="SUCCESS",
        reference_id=f"TOUR_{tournament_id}_{user_to_update.id}"
    )
    
    # Create participant
    new_participant = TournamentParticipant(
        tournament_id=tournament_id,
        user_id=user_to_update.id
    )
    
    db.add(transaction)
    db.add(new_participant)
    db.add(user_to_update)
    db.commit()
    db.refresh(user_to_update)
    
    from services.notifications import add_user_notification
    add_user_notification(
        db, 
        user_to_update.id, 
        "Arena Entry Confirmed", 
        f"You have successfully joined the {db_tournament.title} tournament. Get ready for battle!",
        "TOURNAMENT"
    )
    
    return {
        "message": "Successfully joined the tournament",
        "tournament_id": tournament_id,
        "new_wallet_balance": user_to_update.wallet_balance
    }

@router.get("/my", response_model=List[TournamentResponse])
def get_my_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    participants = db.query(TournamentParticipant).filter(TournamentParticipant.user_id == current_user.id).all()
    tournament_ids = [p.tournament_id for p in participants]
    
    tournaments = db.query(Tournament).filter(Tournament.id.in_(tournament_ids)).all()
    
    # Optional logic: Only expose room details if match_time is near or status is LIVE
    for t in tournaments:
        if t.status != "LIVE":
            t.room_id = None
            t.room_password = None
            
    return tournaments
