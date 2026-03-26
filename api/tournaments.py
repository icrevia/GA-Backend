from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List

from api.deps import get_db, get_current_user, get_current_active_admin
from models.user import User
from models.tournament import Tournament
from models.participant import TournamentParticipant
from models.wallet import WalletTransaction
from schemas.tournament import (
    TournamentCreate, 
    TournamentUpdate, 
    TournamentResponse, 
    TournamentJoinResponse,
    TournamentJoinRequest
)

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
    request: TournamentJoinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    if tournament.status != "UPCOMING":
        raise HTTPException(status_code=400, detail="Tournament is already Live or Completed")
        
    # Check if tournament is full
    participant_count = db.query(TournamentParticipant).filter(TournamentParticipant.tournament_id == tournament_id).count()
    if participant_count >= (tournament.max_slots or 100):
        raise HTTPException(status_code=400, detail="Arena is full! Try another one.")

    # Check if already joined
    existing = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already joined this arena")
        
    # Check and update wallet (atomic)
    user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if user_wallet.wallet_balance < tournament.entry_fee:
        raise HTTPException(status_code=400, detail="Insufficient balance")
        
    # Deduct fee
    user_wallet.wallet_balance -= tournament.entry_fee
    
    # Create transaction log
    transaction = WalletTransaction(
        user_id=current_user.id,
        amount=-tournament.entry_fee,
        transaction_type="JOIN_TOURNAMENT",
        status="SUCCESS",
        reference_id=f"TOUR_{tournament_id}_{current_user.id}"
    )
    db.add(transaction)
    
    # Create participant record
    participant = TournamentParticipant(
        tournament_id=tournament_id, 
        user_id=current_user.id,
        game_username=request.game_username,
        game_uid=request.game_uid
    )
    db.add(participant)
    db.commit()
    
    return {
        "message": f"Successfully joined {tournament.title}!",
        "tournament_id": tournament_id,
        "new_wallet_balance": user_wallet.wallet_balance
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

@router.get("/{tournament_id}", response_model=TournamentResponse)
def get_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
        
    # Check if this user is a participant to show room details
    is_participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id
    ).first()
    
    if not is_participant or tournament.status != "LIVE":
        tournament.room_id = None
        tournament.room_password = None
        
    return tournament
