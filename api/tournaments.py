from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from typing import List

from api.deps import get_current_user, get_current_active_admin
from core.database import get_db_sync as get_db
from models.user import User
from models.tournament import Tournament
from models.participant import TournamentParticipant
from models.wallet import WalletTransaction
from schemas.tournament import (
    TournamentCreate,
    TournamentUpdate,
    TournamentResponse,
    TournamentJoinResponse,
    TournamentJoinRequest,
    TournamentSlotsBoardResponse,
    TournamentSlotResponse,
)
from services.notifications import add_user_notification

router = APIRouter()


def _build_joined_count_map(db: Session, tournament_ids: List[int]) -> dict[int, int]:
    if not tournament_ids:
        return {}

    rows = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(TournamentParticipant.id),
        )
        .filter(TournamentParticipant.tournament_id.in_(tournament_ids))
        .group_by(TournamentParticipant.tournament_id)
        .all()
    )
    return {tid: count for tid, count in rows}


def _attach_joined_counts(tournaments: List[Tournament], db: Session) -> List[Tournament]:
    count_map = _build_joined_count_map(db, [t.id for t in tournaments])
    for tournament in tournaments:
        tournament.joined_count = count_map.get(tournament.id, 0)  # type: ignore[attr-defined]
    return tournaments


def _with_count(tournament: Tournament, db: Session) -> Tournament:
    """Attach joined_count so clients can show slot fill progress."""
    count_map = _build_joined_count_map(db, [tournament.id])
    tournament.joined_count = count_map.get(tournament.id, 0)  # type: ignore[attr-defined]
    return tournament


def _slot_label(slot_no: int) -> str:
    return f"S{slot_no}"


def _expected_team_size(match_type: str | None) -> int:
    mode = (match_type or "SOLO").upper()
    if mode == "DUO":
        return 2
    if mode == "SQUAD":
        return 4
    return 1


def _normalize_join_players(request: TournamentJoinRequest) -> list[dict[str, str]]:
    members: list[dict[str, str]] = []

    if request.players:
        for idx, player in enumerate(request.players, start=1):
            name = (player.name or "").strip()
            uid = (player.uid or "").strip()
            if not name or not uid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Player {idx} requires both name and UID",
                )
            members.append({"name": name, "uid": uid})
        return members

    # Backward compatible fallback for old clients.
    if request.game_username or request.game_uid:
        name = (request.game_username or "").strip()
        uid = (request.game_uid or "").strip()
        if not name or not uid:
            raise HTTPException(status_code=400, detail="Player details must include both name and UID")
        members.append({"name": name, "uid": uid})

    return members


def _validate_team_for_match(match_type: str | None, members: list[dict[str, str]]) -> list[dict[str, str]]:
    mode = (match_type or "SOLO").upper()
    expected = _expected_team_size(mode)

    if len(members) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"{mode} match requires exactly {expected} player name/UID pairs.",
        )

    uids = [member["uid"] for member in members]
    if len(set(uids)) != len(uids):
        raise HTTPException(status_code=400, detail="Each player UID must be unique")

    return members


def _resolve_participant_slots(participants: List[TournamentParticipant], max_slots: int) -> dict[int, TournamentParticipant]:
    slot_map: dict[int, TournamentParticipant] = {}

    for participant in participants:
        slot_no = participant.slot_no
        if slot_no and 1 <= slot_no <= max_slots and slot_no not in slot_map:
            slot_map[slot_no] = participant

    fallback_slot = 1
    for participant in participants:
        slot_no = participant.slot_no
        if slot_no and 1 <= slot_no <= max_slots and slot_no in slot_map and slot_map[slot_no].id == participant.id:
            continue

        while fallback_slot <= max_slots and fallback_slot in slot_map:
            fallback_slot += 1
        if fallback_slot > max_slots:
            break
        slot_map[fallback_slot] = participant

    return slot_map


def _build_slots_board(
    tournament: Tournament,
    participants: List[TournamentParticipant],
    current_user_id: int | None = None,
) -> TournamentSlotsBoardResponse:
    max_slots = max(int(tournament.max_slots or 100), 1)
    slot_map = _resolve_participant_slots(participants, max_slots)

    my_slot_no = None
    if current_user_id is not None:
        for slot_no, participant in slot_map.items():
            if participant.user_id == current_user_id:
                my_slot_no = slot_no
                break

    slots: list[TournamentSlotResponse] = []
    for slot_no in range(1, max_slots + 1):
        participant = slot_map.get(slot_no)
        if participant is None:
            slots.append(
                TournamentSlotResponse(
                    slot_no=slot_no,
                    slot_label=_slot_label(slot_no),
                    status="AVAILABLE",
                )
            )
            continue

        username = participant.username if participant.user else None
        team_members = participant.team_members
        primary_member = team_members[0] if team_members else None
        slots.append(
            TournamentSlotResponse(
                slot_no=slot_no,
                slot_label=_slot_label(slot_no),
                status="BOOKED",
                user_id=participant.user_id,
                username=username,
                avatar_url=(participant.user.profile_pic if participant.user else None),
                bio=(participant.user.bio if participant.user else None),
                game_username=(primary_member["name"] if primary_member else participant.game_username),
                game_uid=(primary_member["uid"] if primary_member else participant.game_uid),
                team_members=team_members,
                is_mine=(current_user_id is not None and participant.user_id == current_user_id),
            )
        )

    return TournamentSlotsBoardResponse(
        tournament_id=tournament.id,
        max_slots=max_slots,
        booked_slots=len(slot_map),
        my_slot_no=my_slot_no,
        my_slot_label=_slot_label(my_slot_no) if my_slot_no else None,
        slots=slots,
    )


def _next_available_slot(db: Session, tournament_id: int, max_slots: int) -> int | None:
    used_rows = db.query(TournamentParticipant.slot_no).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.slot_no.isnot(None),
    ).all()
    used_slots = {int(slot_no) for (slot_no,) in used_rows if slot_no is not None}

    for slot_no in range(1, max_slots + 1):
        if slot_no not in used_slots:
            return slot_no
    return None


@router.get("/", response_model=List[TournamentResponse])
def get_upcoming_tournaments(db: Session = Depends(get_db)):
    tournaments = db.query(Tournament).filter(
        or_(Tournament.status == "UPCOMING", Tournament.status == "LIVE")
    ).order_by(Tournament.match_time.asc()).all()
    return _attach_joined_counts(tournaments, db)


@router.post("/", response_model=TournamentResponse)
def create_tournament(
    tournament_in: TournamentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_admin)
):
    # FIXED: .model_dump() replaces deprecated .dict()
    db_obj = Tournament(**tournament_in.model_dump())
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return _with_count(db_obj, db)


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

    # FIXED: .model_dump() replaces deprecated .dict()
    update_data = tournament_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_obj, field, value)

    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return _with_count(db_obj, db)


@router.post("/{tournament_id}/join", response_model=TournamentJoinResponse)
def join_tournament(
    tournament_id: int,
    request: TournamentJoinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # FIXED: Lock the tournament row first to eliminate the slot race condition
    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()

    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if tournament.status != "UPCOMING":
        raise HTTPException(status_code=400, detail="Tournament is already Live or Completed")

    # Slot check (done after lock — now race-condition-safe)
    participant_count = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id
    ).count()
    max_slots = int(tournament.max_slots or 100)
    if participant_count >= max_slots:
        raise HTTPException(status_code=400, detail="Arena is full! Try another one.")

    # Already joined?
    existing = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Already joined this arena")

    # Lock user row for atomic balance update
    user_wallet = db.query(User).filter(
        User.id == current_user.id
    ).with_for_update().first()

    if user_wallet.wallet_balance < tournament.entry_fee:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Insufficient balance! You need ₹{tournament.entry_fee} to join. Your current balance is ₹{float(user_wallet.wallet_balance):.0f}.",
                "error_code": "INSUFFICIENT_BALANCE",
                "required": float(tournament.entry_fee),
                "available": float(user_wallet.wallet_balance),
            }
        )

    team_members = _validate_team_for_match(
        tournament.match_type,
        _normalize_join_players(request),
    )

    user_wallet.wallet_balance -= tournament.entry_fee

    transaction = WalletTransaction(
        user_id=current_user.id,
        amount=-tournament.entry_fee,
        transaction_type="JOIN_TOURNAMENT",
        status="SUCCESS",
        reference_id=f"TOUR_{tournament_id}_{current_user.id}"
    )
    db.add(transaction)

    slot_no = _next_available_slot(db, tournament_id, max_slots)
    if slot_no is None:
        raise HTTPException(status_code=400, detail="Arena slots unavailable. Please retry.")

    participant = TournamentParticipant(
        tournament_id=tournament_id,
        user_id=current_user.id,
        slot_no=slot_no,
    )
    participant.set_team_members(team_members)
    db.add(participant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Already joined this arena")

    try:
        add_user_notification(
            db,
            current_user.id,
            "Tournament Joined! 🎮",
            f"You have successfully joined '{tournament.title}'. Match starts at {tournament.match_time.strftime('%I:%M %p')}. Stay ready!",
            "APP"
        )
    except Exception: pass

    return {
        "message": f"Successfully joined {tournament.title}!",
        "tournament_id": tournament_id,
        "new_wallet_balance": float(user_wallet.wallet_balance),
        "slot_no": slot_no,
        "slot_label": _slot_label(slot_no),
        "team_members": team_members,
    }


@router.get("/{tournament_id}/slots", response_model=TournamentSlotsBoardResponse)
def get_tournament_slots(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    my_participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id,
    ).first()

    if current_user.role != "ADMIN" and not my_participant:
        raise HTTPException(status_code=403, detail="Join this tournament to view slot board")

    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
    ).all()
    return _build_slots_board(tournament, participants, current_user_id=current_user.id)


@router.get("/my", response_model=List[TournamentResponse])
def get_my_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    participants = db.query(TournamentParticipant).filter(
        TournamentParticipant.user_id == current_user.id
    ).all()
    tournament_ids = [p.tournament_id for p in participants]

    if not tournament_ids:
        return []

    tournaments = db.query(Tournament).filter(Tournament.id.in_(tournament_ids)).all()

    for t in tournaments:
        if t.status != "LIVE":
            t.room_id       = None
            t.room_password = None

    return _attach_joined_counts(tournaments, db)


@router.get("/{tournament_id}", response_model=TournamentResponse)
def get_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    is_participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id
    ).first()

    if not is_participant or tournament.status != "LIVE":
        tournament.room_id       = None
        tournament.room_password = None

    return _with_count(tournament, db)
