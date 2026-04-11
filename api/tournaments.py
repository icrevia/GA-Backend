from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from typing import List
import secrets
import string

from api.deps import get_current_user_tournaments, get_current_active_admin
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
    TeamPreviewResponse,
)
from services.notifications import add_user_notification
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    InsufficientWalletBalanceError,
    debit_wallet,
    get_total_balance,
    to_money,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _generate_join_code(length: int = 6) -> str:
    """Generate a short alphanumeric join code, e.g. 'A3K9PZ'."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _unique_join_code(db: Session, tournament_id: int, length: int = 6) -> str:
    """Generate a join code guaranteed to be unique within this tournament."""
    for _ in range(10):  # max 10 attempts
        code = _generate_join_code(length)
        existing = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.team_join_code == code,
        ).first()
        if not existing:
            return code
    raise HTTPException(status_code=500, detail="Could not generate unique join code. Please retry.")


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


def _normalize_join_players(request: TournamentJoinRequest) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []

    if request.players:
        for idx, player in enumerate(request.players, start=1):
            name = (player.name or "").strip()
            uid = (player.uid or "").strip()
            if not name or not uid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Player {idx} requires both name and UID",
                )
            level = player.level if player.level is not None else request.account_level
            member_payload: dict[str, object] = {"name": name, "uid": uid}
            if level is not None:
                member_payload["level"] = int(level)
            members.append(member_payload)
        return members

    # Backward compatible fallback for old clients.
    if request.game_username or request.game_uid:
        name = (request.game_username or "").strip()
        uid = (request.game_uid or "").strip()
        if not name or not uid:
            raise HTTPException(status_code=400, detail="Player details must include both name and UID")
        member_payload: dict[str, object] = {"name": name, "uid": uid}
        if request.account_level is not None:
            member_payload["level"] = int(request.account_level)
        members.append(member_payload)

    return members


def _validate_team_for_match(match_type: str | None, members: list[dict[str, object]]) -> list[dict[str, object]]:
    mode = (match_type or "SOLO").upper()
    expected = _expected_team_size(mode)

    if len(members) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"{mode} match requires exactly {expected} player name/UID pairs.",
        )

    uids = [str(member["uid"]) for member in members]
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
                account_level=(
                    int(primary_member["level"])
                    if primary_member and primary_member.get("level") is not None
                    else participant.account_level
                ),
                team_members=team_members,
                is_mine=(current_user_id is not None and participant.user_id == current_user_id),
                team_name=participant.team_name,
                team_join_code=participant.team_join_code,
                is_team_captain=bool(participant.is_team_captain),
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


# ─────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[TournamentResponse])
def get_upcoming_tournaments(
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user_tournaments),
):
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


# ─────────────────────────────────────────────────────────────────
# Team Preview — look up a join code before confirming
# ─────────────────────────────────────────────────────────────────

@router.get("/{tournament_id}/team/{join_code}", response_model=TeamPreviewResponse)
def preview_team_by_code(
    tournament_id: int,
    join_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments),
):
    """Let a user preview a team by its join code before paying to join."""
    join_code = join_code.strip().upper()
    tournament = db.query(Tournament).filter(Tournament.id == tournament_id).first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")
    if tournament.status != "UPCOMING":
        raise HTTPException(status_code=400, detail="Tournament is no longer accepting players")

    members = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.team_join_code == join_code,
    ).all()

    if not members:
        raise HTTPException(status_code=404, detail="Invalid join code. Double-check and try again.")

    captain = next((m for m in members if m.is_team_captain), members[0])
    team_size = _expected_team_size(tournament.match_type)
    team_name = members[0].team_name or "—"

    return TeamPreviewResponse(
        team_join_code=join_code,
        team_name=team_name,
        captain_username=captain.username if captain.user else "Unknown",
        current_members=len(members),
        max_members=team_size,
        is_full=len(members) >= team_size,
    )


# ─────────────────────────────────────────────────────────────────
# Join Tournament — handles SOLO / CREATE / JOIN
# ─────────────────────────────────────────────────────────────────

@router.post("/{tournament_id}/join", response_model=TournamentJoinResponse)
def join_tournament(
    tournament_id: int,
    request: TournamentJoinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments)
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

    mode = (tournament.match_type or "SOLO").upper()
    action = (request.action or "").upper() if request.action else None
    is_team_match = mode in ("DUO", "SQUAD")
    team_size = _expected_team_size(mode)

    # ── DUO / SQUAD — CREATE ────────────────────────────────────
    if is_team_match and action == "CREATE":
        team_name = (request.team_name or "").strip()
        if not team_name:
            raise HTTPException(status_code=400, detail="Team name is required to create a team")

        # Only need the captain's own player details (1 member)
        members = _normalize_join_players(request)
        if len(members) != 1:
            raise HTTPException(
                status_code=400,
                detail="Provide only YOUR player name & UID when creating a team"
            )

        # Captain pays the flat team entry fee
        total_fee = to_money(tournament.entry_fee)

        user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
        available_balance = get_total_balance(user_wallet)
        if available_balance < total_fee:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Insufficient balance! You need ₹{total_fee:.2f} to create a team for {mode}. Your current balance is ₹{available_balance:.2f}.",
                    "error_code": "INSUFFICIENT_BALANCE",
                    "required": float(total_fee),
                    "available": float(available_balance),
                }
            )

        try:
            debit_wallet(
                user_wallet,
                total_fee,
                spend_order=(WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING),
            )
        except InsufficientWalletBalanceError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Insufficient balance! You need ₹{exc.required:.2f} to create a team. Your current balance is ₹{exc.available:.2f}.",
                    "error_code": "INSUFFICIENT_BALANCE",
                    "required": float(exc.required),
                    "available": float(exc.available),
                }
            )

        transaction = WalletTransaction(
            user_id=current_user.id,
            amount=-total_fee,
            transaction_type="JOIN_TOURNAMENT",
            status="SUCCESS",
            reference_id=f"TOUR_{tournament_id}_{current_user.id}"
        )
        db.add(transaction)

        slot_no = _next_available_slot(db, tournament_id, max_slots)
        if slot_no is None:
            raise HTTPException(status_code=400, detail="Arena slots unavailable. Please retry.")

        join_code = _unique_join_code(db, tournament_id)

        participant = TournamentParticipant(
            tournament_id=tournament_id,
            user_id=current_user.id,
            slot_no=slot_no,
            account_level=request.account_level,
            team_name=team_name,
            team_join_code=join_code,
            is_team_captain=True,
        )
        participant.set_team_members(members)
        db.add(participant)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=400, detail="Already joined this arena")

        try:
            add_user_notification(
                db, current_user.id,
                "Team Created! 🎮",
                f"Your team '{team_name}' is set for '{tournament.title}'. Share code {join_code} with your teammates!",
                "APP"
            )
        except Exception:
            pass

        return {
            "message": f"Team '{team_name}' created for {tournament.title}! Share code: {join_code}",
            "tournament_id": tournament_id,
            "new_wallet_balance": float(get_total_balance(user_wallet)),
            "slot_no": slot_no,
            "slot_label": _slot_label(slot_no),
            "team_members": members,
            "team_join_code": join_code,
            "team_name": team_name,
            "is_team_captain": True,
        }

    # ── DUO / SQUAD — JOIN ───────────────────────────────────────
    if is_team_match and action == "JOIN":
        join_code = (request.join_code or "").strip().upper()
        if not join_code:
            raise HTTPException(status_code=400, detail="Join code is required to join a team")

        # Find existing team members
        team_members_in_db = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.team_join_code == join_code,
        ).all()

        if not team_members_in_db:
            raise HTTPException(status_code=404, detail="Invalid join code. Double-check and try again.")

        if len(team_members_in_db) >= team_size:
            raise HTTPException(
                status_code=400,
                detail=f"This team is already full ({team_size}/{team_size} members)."
            )

        existing_team_name = team_members_in_db[0].team_name or ""

        # Joiner does NOT pay — captain already paid for the entire team
        slot_no = _next_available_slot(db, tournament_id, max_slots)
        if slot_no is None:
            raise HTTPException(status_code=400, detail="Arena slots unavailable. Please retry.")

        # Joiner still needs their player info (name + uid)
        member_payload: dict[str, object] = {}
        if request.players and len(request.players) > 0:
            p = request.players[0]
            name = (p.name or "").strip()
            uid = (p.uid or "").strip()
            if not name or not uid:
                raise HTTPException(status_code=400, detail="Your player name and UID are required")
            member_payload = {"name": name, "uid": uid}
            if p.level is not None:
                member_payload["level"] = int(p.level)
        elif request.game_username and request.game_uid:
            name = request.game_username.strip()
            uid = request.game_uid.strip()
            member_payload = {"name": name, "uid": uid}
            if request.account_level is not None:
                member_payload["level"] = int(request.account_level)
        else:
            raise HTTPException(status_code=400, detail="Your player name and UID are required to join a team")

        participant = TournamentParticipant(
            tournament_id=tournament_id,
            user_id=current_user.id,
            slot_no=slot_no,
            account_level=request.account_level,
            team_name=existing_team_name,
            team_join_code=join_code,
            is_team_captain=False,
        )
        participant.set_team_members([member_payload])
        db.add(participant)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=400, detail="Already joined this arena")

        # Get current wallet balance (no deduction for joiners)
        user_wallet = db.query(User).filter(User.id == current_user.id).first()

        try:
            add_user_notification(
                db, current_user.id,
                "Joined Team! 🎮",
                f"You've joined team '{existing_team_name}' for '{tournament.title}'. Get ready!",
                "APP"
            )
        except Exception:
            pass

        return {
            "message": f"You've joined team '{existing_team_name}' in {tournament.title}!",
            "tournament_id": tournament_id,
            "new_wallet_balance": float(get_total_balance(user_wallet)) if user_wallet else 0.0,
            "slot_no": slot_no,
            "slot_label": _slot_label(slot_no),
            "team_members": [member_payload],
            "team_join_code": join_code,
            "team_name": existing_team_name,
            "is_team_captain": False,
        }

    # ── SOLO (or legacy DUO/SQUAD with all members) ──────────────
    user_wallet = db.query(User).filter(
        User.id == current_user.id
    ).with_for_update().first()

    entry_fee = to_money(tournament.entry_fee)
    available_balance = get_total_balance(user_wallet)

    if available_balance < entry_fee:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Insufficient balance! You need ₹{entry_fee:.2f} to join. Your current balance is ₹{available_balance:.2f}.",
                "error_code": "INSUFFICIENT_BALANCE",
                "required": float(entry_fee),
                "available": float(available_balance),
            }
        )

    team_members = _validate_team_for_match(
        tournament.match_type,
        _normalize_join_players(request),
    )

    try:
        debit_wallet(
            user_wallet,
            entry_fee,
            spend_order=(
                WALLET_BUCKET_BONUS,
                WALLET_BUCKET_DEPOSIT,
                WALLET_BUCKET_WINNING,
            ),
        )
    except InsufficientWalletBalanceError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Insufficient balance! You need ₹{exc.required:.2f} to join. Your current balance is ₹{exc.available:.2f}.",
                "error_code": "INSUFFICIENT_BALANCE",
                "required": float(exc.required),
                "available": float(exc.available),
            }
        )

    transaction = WalletTransaction(
        user_id=current_user.id,
        amount=-entry_fee,
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
        account_level=(request.account_level if request.account_level is not None else None),
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
        "new_wallet_balance": float(get_total_balance(user_wallet)),
        "slot_no": slot_no,
        "slot_label": _slot_label(slot_no),
        "team_members": team_members,
        "team_join_code": None,
        "team_name": None,
        "is_team_captain": False,
    }


@router.get("/{tournament_id}/slots", response_model=TournamentSlotsBoardResponse)
def get_tournament_slots(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments),
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
    current_user: User = Depends(get_current_user_tournaments)
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
    current_user: User = Depends(get_current_user_tournaments)
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
