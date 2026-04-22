from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from typing import List
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import secrets
import string
import uuid

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
    TournamentCancelResponse,
    TournamentSlotsBoardResponse,
    TournamentSlotResponse,
    TeamPreviewResponse,
)
from services.notifications import add_user_notification
from services.daily_bonus_usage import (
    get_daily_bonus_allowance,
    reduce_bonus_usage,
    register_bonus_usage,
)
from services.wallet_balances import (
    WALLET_BUCKET_BONUS,
    WALLET_BUCKET_DEPOSIT,
    WALLET_BUCKET_WINNING,
    InsufficientWalletBalanceError,
    credit_wallet,
    debit_wallet,
    get_total_balance,
    to_money,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _generate_join_code(length: int = 6) -> str:
    """Generate a short numeric join code, e.g. '839210'."""
    alphabet = string.digits
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
            func.count(func.distinct(TournamentParticipant.slot_no)),
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


def _resolve_participant_slots(participants: List[TournamentParticipant], max_slots: int) -> dict[int, List[TournamentParticipant]]:
    slot_map: dict[int, List[TournamentParticipant]] = {}

    # First pass: put participants in their designated slots
    for participant in participants:
        slot_no = participant.slot_no
        if slot_no and 1 <= slot_no <= max_slots:
            if slot_no not in slot_map:
                slot_map[slot_no] = []
            slot_map[slot_no].append(participant)

    # Second pass: handle participants without slot_no (legacy or edge cases)
    fallback_slot = 1
    for participant in participants:
        if participant.slot_no and 1 <= participant.slot_no <= max_slots:
            continue

        while fallback_slot <= max_slots and fallback_slot in slot_map:
            fallback_slot += 1
        if fallback_slot > max_slots:
            break
        slot_map[fallback_slot] = [participant]
        participant.slot_no = fallback_slot # Update for current session

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
        for slot_no, slot_participants in slot_map.items():
            if any(p.user_id == current_user_id for p in slot_participants):
                my_slot_no = slot_no
                break

    slots: list[TournamentSlotResponse] = []
    for slot_no in range(1, max_slots + 1):
        slot_participants = slot_map.get(slot_no)
        if not slot_participants:
            slots.append(
                TournamentSlotResponse(
                    slot_no=slot_no,
                    slot_label=_slot_label(slot_no),
                    status="AVAILABLE",
                )
            )
            continue

        # Merge all team members from all participants in this slot
        merged_team_members = []
        for p in slot_participants:
            merged_team_members.extend(p.team_members)

        # Pick the "primary" user for display (Captain or first joined)
        primary_p = next((p for p in slot_participants if p.is_team_captain), slot_participants[0])
        
        username = primary_p.username if primary_p.user else None
        slots.append(
            TournamentSlotResponse(
                slot_no=slot_no,
                slot_label=_slot_label(slot_no),
                status="BOOKED",
                user_id=primary_p.user_id,
                username=username,
                avatar_url=(primary_p.user.profile_pic if primary_p.user else None),
                bio=(primary_p.user.bio if primary_p.user else None),
                game_username=None, # Multiple members now
                game_uid=None,
                account_level=primary_p.account_level,
                team_members=merged_team_members,
                is_mine=(current_user_id is not None and any(p.user_id == current_user_id for p in slot_participants)),
                team_name=primary_p.team_name,
                team_join_code=primary_p.team_join_code,
                is_team_captain=any(p.is_team_captain for p in slot_participants),
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


def _now_for_match_timezone(match_time: datetime) -> datetime:
    now_utc = datetime.now(timezone.utc)
    if match_time.tzinfo is None:
        return now_utc.replace(tzinfo=None)
    return now_utc.astimezone(match_time.tzinfo)


def _resolve_bonus_usage_limit_percentage(tournament: Tournament) -> Decimal:
    raw = to_money(getattr(tournament, "commission_percentage", 0) or 0)
    if raw < Decimal("0.00"):
        return Decimal("0.00")
    if raw > Decimal("100.00"):
        return Decimal("100.00")
    return raw


def _compute_join_wallet_deductions(
    user_wallet: User,
    total_fee: Decimal,
    bonus_usage_limit_percentage: Decimal,
    daily_bonus_remaining: Decimal | None,
) -> tuple[dict[str, Decimal], Decimal, Decimal, Decimal, Decimal]:
    fee = to_money(total_fee)
    limit_pct = to_money(bonus_usage_limit_percentage)
    per_match_bonus_cap_amount = to_money((fee * limit_pct) / Decimal("100.00"))
    effective_bonus_cap_amount = per_match_bonus_cap_amount

    if daily_bonus_remaining is not None:
        effective_bonus_cap_amount = min(effective_bonus_cap_amount, to_money(daily_bonus_remaining))

    available_bonus = to_money(getattr(user_wallet, "bonus_balance", Decimal("0.00")))
    available_deposit = to_money(getattr(user_wallet, "deposit_balance", Decimal("0.00")))
    available_winning = to_money(getattr(user_wallet, "winning_balance", Decimal("0.00")))

    potential_bonus_take_without_daily_cap = min(available_bonus, per_match_bonus_cap_amount, fee)
    bonus_take = min(available_bonus, effective_bonus_cap_amount, fee)
    remaining_after_bonus = to_money(fee - bonus_take)

    daily_bonus_blocked_amount = Decimal("0.00")
    if daily_bonus_remaining is not None and potential_bonus_take_without_daily_cap > bonus_take:
        daily_bonus_blocked_amount = to_money(potential_bonus_take_without_daily_cap - bonus_take)

    deposit_take = min(available_deposit, remaining_after_bonus)
    remaining_after_deposit = to_money(remaining_after_bonus - deposit_take)

    winning_take = min(available_winning, remaining_after_deposit)
    remaining_due = to_money(remaining_after_deposit - winning_take)

    deductions = {
        WALLET_BUCKET_BONUS: to_money(bonus_take),
        WALLET_BUCKET_DEPOSIT: to_money(deposit_take),
        WALLET_BUCKET_WINNING: to_money(winning_take),
    }
    return (
        deductions,
        per_match_bonus_cap_amount,
        effective_bonus_cap_amount,
        remaining_due,
        daily_bonus_blocked_amount,
    )


def _apply_join_wallet_deductions(user_wallet: User, deductions: dict[str, Decimal]) -> None:
    bonus_amount = to_money(deductions.get(WALLET_BUCKET_BONUS))
    deposit_amount = to_money(deductions.get(WALLET_BUCKET_DEPOSIT))
    winning_amount = to_money(deductions.get(WALLET_BUCKET_WINNING))

    if bonus_amount > Decimal("0.00"):
        debit_wallet(user_wallet, bonus_amount, spend_order=(WALLET_BUCKET_BONUS,))
    if deposit_amount > Decimal("0.00"):
        debit_wallet(user_wallet, deposit_amount, spend_order=(WALLET_BUCKET_DEPOSIT,))
    if winning_amount > Decimal("0.00"):
        debit_wallet(user_wallet, winning_amount, spend_order=(WALLET_BUCKET_WINNING,))


def _join_deduction_payload(
    deductions: dict[str, Decimal],
    bonus_cap_amount: Decimal,
    bonus_usage_limit_percentage: Decimal,
    daily_bonus_limit_amount: Decimal | None = None,
    daily_bonus_used_today: Decimal | None = None,
    daily_bonus_remaining_today: Decimal | None = None,
    daily_bonus_blocked_amount: Decimal | None = None,
) -> dict[str, float | None]:
    total_deducted = to_money(
        to_money(deductions.get(WALLET_BUCKET_BONUS))
        + to_money(deductions.get(WALLET_BUCKET_DEPOSIT))
        + to_money(deductions.get(WALLET_BUCKET_WINNING))
    )
    return {
        "bonus_amount": float(to_money(deductions.get(WALLET_BUCKET_BONUS))),
        "deposit_amount": float(to_money(deductions.get(WALLET_BUCKET_DEPOSIT))),
        "winning_amount": float(to_money(deductions.get(WALLET_BUCKET_WINNING))),
        "total_deducted": float(total_deducted),
        "bonus_cap_amount": float(to_money(bonus_cap_amount)),
        "bonus_usage_limit_percentage": float(to_money(bonus_usage_limit_percentage)),
        "daily_bonus_limit_amount": (
            float(to_money(daily_bonus_limit_amount))
            if daily_bonus_limit_amount is not None
            else None
        ),
        "daily_bonus_used_today": (
            float(to_money(daily_bonus_used_today))
            if daily_bonus_used_today is not None
            else None
        ),
        "daily_bonus_remaining_today": (
            float(to_money(daily_bonus_remaining_today))
            if daily_bonus_remaining_today is not None
            else None
        ),
        "daily_bonus_blocked_amount": (
            float(to_money(daily_bonus_blocked_amount))
            if daily_bonus_blocked_amount is not None
            else None
        ),
    }


def _build_join_failure_reason(tournament_id: int, deductions: dict[str, Decimal]) -> str:
    return (
        f"TOUR:{tournament_id};"
        f"DEDUCT_BONUS:{to_money(deductions.get(WALLET_BUCKET_BONUS)):.2f};"
        f"DEDUCT_DEPOSIT:{to_money(deductions.get(WALLET_BUCKET_DEPOSIT)):.2f};"
        f"DEDUCT_WINNING:{to_money(deductions.get(WALLET_BUCKET_WINNING)):.2f}"
    )


def _normalize_wallet_distribution(
    distribution: dict[str, Decimal],
    target_total: Decimal,
) -> dict[str, Decimal]:
    target = to_money(target_total)
    normalized = {
        WALLET_BUCKET_BONUS: to_money(distribution.get(WALLET_BUCKET_BONUS)),
        WALLET_BUCKET_DEPOSIT: to_money(distribution.get(WALLET_BUCKET_DEPOSIT)),
        WALLET_BUCKET_WINNING: to_money(distribution.get(WALLET_BUCKET_WINNING)),
    }

    if target <= Decimal("0.00"):
        return {key: Decimal("0.00") for key in normalized}

    current_total = to_money(sum(normalized.values(), Decimal("0.00")))
    if current_total <= Decimal("0.00"):
        return {
            WALLET_BUCKET_BONUS: Decimal("0.00"),
            WALLET_BUCKET_DEPOSIT: target,
            WALLET_BUCKET_WINNING: Decimal("0.00"),
        }

    scaled = {
        key: to_money((amount / current_total) * target)
        for key, amount in normalized.items()
    }
    scaled_total = to_money(sum(scaled.values(), Decimal("0.00")))
    diff = to_money(target - scaled_total)
    if diff != Decimal("0.00"):
        anchor_bucket = max(normalized, key=lambda key: normalized[key])
        scaled[anchor_bucket] = to_money(scaled[anchor_bucket] + diff)

    return scaled


def _parse_join_deduction_distribution(
    failure_reason: str | None,
    tournament_id: int,
    fallback_total: Decimal,
) -> dict[str, Decimal]:
    parsed = {
        WALLET_BUCKET_BONUS: Decimal("0.00"),
        WALLET_BUCKET_DEPOSIT: Decimal("0.00"),
        WALLET_BUCKET_WINNING: Decimal("0.00"),
    }

    reason_text = str(failure_reason or "")
    expected_tour_token = f"TOUR:{tournament_id}"
    if expected_tour_token not in reason_text:
        return _normalize_wallet_distribution(parsed, fallback_total)

    for token in reason_text.split(";"):
        token = token.strip()
        try:
            if token.startswith("DEDUCT_BONUS:"):
                parsed[WALLET_BUCKET_BONUS] = to_money(token.split(":", 1)[1] or "0")
            elif token.startswith("DEDUCT_DEPOSIT:"):
                parsed[WALLET_BUCKET_DEPOSIT] = to_money(token.split(":", 1)[1] or "0")
            elif token.startswith("DEDUCT_WINNING:"):
                parsed[WALLET_BUCKET_WINNING] = to_money(token.split(":", 1)[1] or "0")
        except Exception:
            continue

    return _normalize_wallet_distribution(parsed, fallback_total)


def _scaled_refund_distribution(
    original_distribution: dict[str, Decimal],
    ratio: Decimal,
) -> dict[str, Decimal]:
    original_total = to_money(sum(original_distribution.values(), Decimal("0.00")))
    refund_total = to_money(original_total * to_money(ratio))
    return _normalize_wallet_distribution(original_distribution, refund_total)


def _format_wallet_refund_breakdown(refund_distribution: dict[str, Decimal]) -> str:
    parts: list[str] = []
    bonus_amount = to_money(refund_distribution.get(WALLET_BUCKET_BONUS))
    deposit_amount = to_money(refund_distribution.get(WALLET_BUCKET_DEPOSIT))
    winning_amount = to_money(refund_distribution.get(WALLET_BUCKET_WINNING))

    if bonus_amount > Decimal("0.00"):
        parts.append(f"bonus ₹{bonus_amount:.2f}")
    if deposit_amount > Decimal("0.00"):
        parts.append(f"deposit ₹{deposit_amount:.2f}")
    if winning_amount > Decimal("0.00"):
        parts.append(f"winning ₹{winning_amount:.2f}")
    return ", ".join(parts) if parts else "no wallet credit"


# ─────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[TournamentResponse])
def get_upcoming_tournaments(
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user_tournaments),
):
    joined_subq = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(func.distinct(TournamentParticipant.slot_no)).label('j_count')
        )
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )

    rows = (
        db.query(Tournament, func.coalesce(joined_subq.c.j_count, 0))
        .outerjoin(joined_subq, Tournament.id == joined_subq.c.tournament_id)
        .filter(or_(Tournament.status == "UPCOMING", Tournament.status == "LIVE"))
        .order_by(Tournament.match_time.asc())
        .all()
    )

    result = []
    for t, count in rows:
        t.joined_count = count
        result.append(t)
        
    return result


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

    old_status = db_obj.status  # capture before update
    update_data = tournament_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_obj, field, value)

    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)

    # ── Push notifications on status change ──────────────────────
    new_status = db_obj.status
    if new_status != old_status and new_status in ("LIVE", "COMPLETED"):
        try:
            from services.push_notifications import send_push_to_many
            participants = db.query(TournamentParticipant).filter(
                TournamentParticipant.tournament_id == tournament_id
            ).all()
            user_ids = list({p.user_id for p in participants})
            if user_ids:
                tokens = [
                    u.fcm_token for u in
                    db.query(User).filter(User.id.in_(user_ids), User.fcm_token.isnot(None)).all()
                    if u.fcm_token
                ]
                if tokens:
                    title_map = {
                        "LIVE":      f"🔴 Match is LIVE! — {db_obj.title}",
                        "COMPLETED": f"🏆 Results Out! — {db_obj.title}",
                    }
                    body_map = {
                        "LIVE":      "The match has started. Open the app immediately!",
                        "COMPLETED": "The match has ended. Check your result and winnings! 💰",
                    }
                    import threading
                    threading.Thread(
                        target=send_push_to_many,
                        args=(tokens, title_map[new_status], body_map[new_status]),
                        kwargs={"data": {"tournament_id": str(tournament_id), "status": new_status}},
                        daemon=True,
                    ).start()
        except Exception as notif_err:
            import logging
            logging.getLogger("GamerzAdda").warning("Push notification error: %s", notif_err)
    # ─────────────────────────────────────────────────────────────

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
        bonus_usage_limit_percentage = _resolve_bonus_usage_limit_percentage(tournament)

        user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
        bonus_cycle_key, daily_bonus_limit_amount, daily_bonus_used_today, daily_bonus_remaining = get_daily_bonus_allowance(
            db,
            user_wallet,
        )
        (
            deductions,
            per_match_bonus_cap_amount,
            bonus_cap_amount,
            remaining_due,
            daily_bonus_blocked_amount,
        ) = _compute_join_wallet_deductions(
            user_wallet,
            total_fee,
            bonus_usage_limit_percentage,
            daily_bonus_remaining,
        )
        available_by_rule = to_money(total_fee - remaining_due)
        if remaining_due > Decimal("0.00"):
            if (
                daily_bonus_remaining is not None
                and daily_bonus_remaining <= Decimal("0.00")
                and daily_bonus_blocked_amount > Decimal("0.00")
            ):
                message = (
                    f"Daily bonus limit reached for today. Limit ₹{daily_bonus_limit_amount:.2f}, "
                    f"used ₹{daily_bonus_used_today:.2f}. Bonus wallet cannot be used further today. "
                    f"You can pay ₹{available_by_rule:.2f} from deposit/winning wallet right now."
                )
            else:
                message = (
                    f"Insufficient balance! For this match, max bonus usage is {float(bonus_usage_limit_percentage):.2f}% "
                    f"(₹{per_match_bonus_cap_amount:.2f}). You can pay ₹{available_by_rule:.2f} right now."
                )
                if daily_bonus_remaining is not None:
                    message += (
                        f" Daily bonus remaining today: ₹{daily_bonus_remaining:.2f} "
                        f"(limit ₹{daily_bonus_limit_amount:.2f}, used ₹{daily_bonus_used_today:.2f})."
                    )
            raise HTTPException(
                status_code=400,
                detail={
                    "message": message,
                    "error_code": "INSUFFICIENT_BALANCE",
                    "required": float(total_fee),
                    "available": float(available_by_rule),
                    "wallet_total": float(get_total_balance(user_wallet)),
                    "bonus_cap_amount": float(bonus_cap_amount),
                    "bonus_usage_limit_percentage": float(bonus_usage_limit_percentage),
                    "daily_bonus_limit_amount": float(daily_bonus_limit_amount),
                    "daily_bonus_used_today": float(daily_bonus_used_today),
                    "daily_bonus_remaining_today": (
                        float(daily_bonus_remaining) if daily_bonus_remaining is not None else None
                    ),
                }
            )

        try:
            _apply_join_wallet_deductions(user_wallet, deductions)
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

        register_bonus_usage(
            user_wallet,
            to_money(deductions.get(WALLET_BUCKET_BONUS)),
            cycle_key=bonus_cycle_key,
        )

        transaction = WalletTransaction(
            user_id=current_user.id,
            amount=-total_fee,
            transaction_type="JOIN_TOURNAMENT",
            status="SUCCESS",
            reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
            failure_reason=_build_join_failure_reason(tournament_id, deductions),
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

        success_message = f"Team '{team_name}' created for {tournament.title}! Share code: {join_code}"
        if daily_bonus_blocked_amount > Decimal("0.00"):
            success_message = (
                f"Daily bonus limit reached for today. Only allowed bonus was used; "
                f"₹{daily_bonus_blocked_amount:.2f} extra bonus could not be used. "
                f"{success_message}"
            )

        return {
            "message": success_message,
            "tournament_id": tournament_id,
            "new_wallet_balance": float(get_total_balance(user_wallet)),
            "slot_no": slot_no,
            "slot_label": _slot_label(slot_no),
            "team_members": members,
            "team_join_code": join_code,
            "team_name": team_name,
            "is_team_captain": True,
            "deduction_breakdown": _join_deduction_payload(
                deductions,
                bonus_cap_amount,
                bonus_usage_limit_percentage,
                daily_bonus_limit_amount=daily_bonus_limit_amount,
                daily_bonus_used_today=daily_bonus_used_today,
                daily_bonus_remaining_today=daily_bonus_remaining,
                daily_bonus_blocked_amount=daily_bonus_blocked_amount,
            ),
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
        
        # SECURITY: Lock the team captain's record to prevent team overfill race conditions
        captain_record = next((m for m in team_members_in_db if m.is_team_captain), team_members_in_db[0])
        db.query(TournamentParticipant).filter(TournamentParticipant.id == captain_record.id).with_for_update().first()

        # RE-CHECK team size after lock
        current_team_count = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.team_join_code == join_code,
        ).count()
        if current_team_count >= team_size:
            raise HTTPException(
                status_code=400,
                detail=f"This team is already full ({team_size}/{team_size} members)."
            )

        # MANDATORY: Every team member must pay the entry fee
        entry_fee = to_money(tournament.entry_fee)
        bonus_usage_limit_percentage = _resolve_bonus_usage_limit_percentage(tournament)
        
        user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
        bonus_cycle_key, daily_bonus_limit_amount, daily_bonus_used_today, daily_bonus_remaining = get_daily_bonus_allowance(
            db,
            user_wallet,
        )
        (
            deductions,
            per_match_bonus_cap_amount,
            bonus_cap_amount,
            remaining_due,
            daily_bonus_blocked_amount,
        ) = _compute_join_wallet_deductions(
            user_wallet,
            entry_fee,
            bonus_usage_limit_percentage,
            daily_bonus_remaining,
        )
        
        available_by_rule = to_money(entry_fee - remaining_due)
        if remaining_due > Decimal("0.00"):
            # Reuse the same error message logic as SOLO/CREATE
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Insufficient balance! You need ₹{float(entry_fee):.2f} to join this team.",
                    "error_code": "INSUFFICIENT_BALANCE",
                    "required": float(entry_fee),
                    "available": float(available_by_rule),
                }
            )

        try:
            _apply_join_wallet_deductions(user_wallet, deductions)
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

        register_bonus_usage(
            user_wallet,
            to_money(deductions.get(WALLET_BUCKET_BONUS)),
            cycle_key=bonus_cycle_key,
        )

        transaction = WalletTransaction(
            user_id=current_user.id,
            amount=-entry_fee,
            transaction_type="JOIN_TOURNAMENT",
            status="SUCCESS",
            reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
            failure_reason=_build_join_failure_reason(tournament_id, deductions),
        )
        db.add(transaction)

        # Inherit the slot number from the captain
        slot_no = captain_record.slot_no
        if slot_no is None:
            # Fallback if somehow missing
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
            "new_wallet_balance": float(get_total_balance(user_wallet)),
            "slot_no": slot_no,
            "slot_label": _slot_label(slot_no),
            "team_members": [member_payload],
            "team_join_code": join_code,
            "team_name": existing_team_name,
            "is_team_captain": False,
            "deduction_breakdown": _join_deduction_payload(
                deductions,
                bonus_cap_amount,
                bonus_usage_limit_percentage,
                daily_bonus_limit_amount=daily_bonus_limit_amount,
                daily_bonus_used_today=daily_bonus_used_today,
                daily_bonus_remaining_today=daily_bonus_remaining,
                daily_bonus_blocked_amount=daily_bonus_blocked_amount,
            ),
        }

    # ── SOLO (or legacy DUO/SQUAD with all members) ──────────────
    user_wallet = db.query(User).filter(
        User.id == current_user.id
    ).with_for_update().first()

    entry_fee = to_money(tournament.entry_fee)
    bonus_usage_limit_percentage = _resolve_bonus_usage_limit_percentage(tournament)
    bonus_cycle_key, daily_bonus_limit_amount, daily_bonus_used_today, daily_bonus_remaining = get_daily_bonus_allowance(
        db,
        user_wallet,
    )
    (
        deductions,
        per_match_bonus_cap_amount,
        bonus_cap_amount,
        remaining_due,
        daily_bonus_blocked_amount,
    ) = _compute_join_wallet_deductions(
        user_wallet,
        entry_fee,
        bonus_usage_limit_percentage,
        daily_bonus_remaining,
    )
    available_by_rule = to_money(entry_fee - remaining_due)

    if remaining_due > Decimal("0.00"):
        if (
            daily_bonus_remaining is not None
            and daily_bonus_remaining <= Decimal("0.00")
            and daily_bonus_blocked_amount > Decimal("0.00")
        ):
            message = (
                f"Daily bonus limit reached for today. Limit ₹{daily_bonus_limit_amount:.2f}, "
                f"used ₹{daily_bonus_used_today:.2f}. Bonus wallet cannot be used further today. "
                f"You can pay ₹{available_by_rule:.2f} from deposit/winning wallet right now."
            )
        else:
            message = (
                f"Insufficient balance! For this match, max bonus usage is {float(bonus_usage_limit_percentage):.2f}% "
                f"(₹{per_match_bonus_cap_amount:.2f}). You can pay ₹{available_by_rule:.2f} right now."
            )
            if daily_bonus_remaining is not None:
                message += (
                    f" Daily bonus remaining today: ₹{daily_bonus_remaining:.2f} "
                    f"(limit ₹{daily_bonus_limit_amount:.2f}, used ₹{daily_bonus_used_today:.2f})."
                )
        raise HTTPException(
            status_code=400,
            detail={
                "message": message,
                "error_code": "INSUFFICIENT_BALANCE",
                "required": float(entry_fee),
                "available": float(available_by_rule),
                "wallet_total": float(get_total_balance(user_wallet)),
                "bonus_cap_amount": float(bonus_cap_amount),
                "bonus_usage_limit_percentage": float(bonus_usage_limit_percentage),
                "daily_bonus_limit_amount": float(daily_bonus_limit_amount),
                "daily_bonus_used_today": float(daily_bonus_used_today),
                "daily_bonus_remaining_today": (
                    float(daily_bonus_remaining) if daily_bonus_remaining is not None else None
                ),
            }
        )

    team_members = _validate_team_for_match(
        tournament.match_type,
        _normalize_join_players(request),
    )

    try:
        _apply_join_wallet_deductions(user_wallet, deductions)
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

    register_bonus_usage(
        user_wallet,
        to_money(deductions.get(WALLET_BUCKET_BONUS)),
        cycle_key=bonus_cycle_key,
    )

    transaction = WalletTransaction(
        user_id=current_user.id,
        amount=-entry_fee,
        transaction_type="JOIN_TOURNAMENT",
        status="SUCCESS",
        reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
        failure_reason=_build_join_failure_reason(tournament_id, deductions),
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

    success_message = f"Successfully joined {tournament.title}!"
    if daily_bonus_blocked_amount > Decimal("0.00"):
        success_message = (
            f"Daily bonus limit reached for today. Only allowed bonus was used; "
            f"₹{daily_bonus_blocked_amount:.2f} extra bonus could not be used. "
            f"{success_message}"
        )

    return {
        "message": success_message,
        "tournament_id": tournament_id,
        "new_wallet_balance": float(get_total_balance(user_wallet)),
        "slot_no": slot_no,
        "slot_label": _slot_label(slot_no),
        "team_members": team_members,
        "team_join_code": None,
        "team_name": None,
        "is_team_captain": False,
        "deduction_breakdown": _join_deduction_payload(
            deductions,
            bonus_cap_amount,
            bonus_usage_limit_percentage,
            daily_bonus_limit_amount=daily_bonus_limit_amount,
            daily_bonus_used_today=daily_bonus_used_today,
            daily_bonus_remaining_today=daily_bonus_remaining,
            daily_bonus_blocked_amount=daily_bonus_blocked_amount,
        ),
    }


@router.post("/{tournament_id}/cancel", response_model=TournamentCancelResponse)
def cancel_tournament_participation(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments),
):
    tournament = db.query(Tournament).filter(
        Tournament.id == tournament_id
    ).with_for_update().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if (tournament.status or "").upper() != "UPCOMING":
        raise HTTPException(status_code=400, detail="Only upcoming tournaments can be cancelled")

    participant = db.query(TournamentParticipant).filter(
        TournamentParticipant.tournament_id == tournament_id,
        TournamentParticipant.user_id == current_user.id,
    ).with_for_update().first()
    if not participant:
        raise HTTPException(status_code=404, detail="You are not part of this tournament")

    if not tournament.match_time:
        raise HTTPException(status_code=400, detail="Tournament match time is missing")

    cancel_cutoff = tournament.match_time - timedelta(hours=2)
    now_for_match = _now_for_match_timezone(tournament.match_time)
    if now_for_match >= cancel_cutoff:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Cancellation closes 2 hours before match start.",
                "error_code": "CANCEL_WINDOW_CLOSED",
            },
        )

    mode = (tournament.match_type or "SOLO").upper()
    is_team_match = mode in ("DUO", "SQUAD")
    is_captain_cancel = bool(is_team_match and participant.is_team_captain and participant.team_join_code)

    participants_to_remove = [participant]
    if is_captain_cancel and participant.team_join_code:
        participants_to_remove = db.query(TournamentParticipant).filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.team_join_code == participant.team_join_code,
        ).with_for_update().all()
        if not participants_to_remove:
            participants_to_remove = [participant]

    paid_amount = Decimal("0.00")
    join_tx: WalletTransaction | None = None
    original_deduction_distribution = {
        WALLET_BUCKET_BONUS: Decimal("0.00"),
        WALLET_BUCKET_DEPOSIT: Decimal("0.00"),
        WALLET_BUCKET_WINNING: Decimal("0.00"),
    }

    if not is_team_match or is_captain_cancel:
        join_tx = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == current_user.id,
            WalletTransaction.transaction_type == "JOIN_TOURNAMENT",
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.contains(f"TOUR:{tournament_id}"),
        ).order_by(WalletTransaction.id.desc()).first()

        if join_tx:
            paid_amount = to_money(abs(join_tx.amount))
        else:
            paid_amount = to_money(tournament.entry_fee)

        original_deduction_distribution = _parse_join_deduction_distribution(
            join_tx.failure_reason if join_tx else None,
            tournament_id,
            paid_amount,
        )

    refund_distribution = _scaled_refund_distribution(original_deduction_distribution, Decimal("0.70"))
    refund_amount = to_money(sum(refund_distribution.values(), Decimal("0.00")))

    user_wallet = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if not user_wallet:
        raise HTTPException(status_code=404, detail="User not found")

    if refund_amount > Decimal("0.00"):
        join_reference_token = "JOIN_UNKNOWN"
        if join_tx is not None:
            join_reference_token = (join_tx.reference_id or "").strip() or f"JOINTX:{join_tx.id}"
        else:
            join_reference_token = f"PARTICIPANT:{participant.id}"

        cancel_dedup_key = f"CANCEL:{tournament_id}:{current_user.id}:{join_reference_token}"
        existing_refund = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == current_user.id,
            WalletTransaction.transaction_type == "TOURNAMENT_CANCEL_REFUND",
            WalletTransaction.status == "SUCCESS",
            WalletTransaction.failure_reason.contains(cancel_dedup_key),
        ).first()
        if existing_refund:
            raise HTTPException(status_code=409, detail="Cancellation refund already processed")

        for bucket in (WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING):
            bucket_refund = to_money(refund_distribution.get(bucket))
            if bucket_refund <= Decimal("0.00"):
                continue

            credit_wallet(user_wallet, bucket_refund, bucket)
            db.add(
                WalletTransaction(
                    user_id=current_user.id,
                    amount=bucket_refund,
                    transaction_type="TOURNAMENT_CANCEL_REFUND",
                    status="SUCCESS",
                    reference_id=f"GA-{uuid.uuid4().hex[:6].upper()}",
                    failure_reason=(
                        f"{cancel_dedup_key};BUCKET:{bucket};"
                        f"REFUND_PERCENT:70"
                    ),
                )
            )

        bonus_refund_amount = to_money(refund_distribution.get(WALLET_BUCKET_BONUS))
        if bonus_refund_amount > Decimal("0.00"):
            reduce_bonus_usage(user_wallet, bonus_refund_amount)

    teammate_user_ids: list[int] = []
    if is_captain_cancel:
        teammate_user_ids = [p.user_id for p in participants_to_remove if p.user_id != current_user.id]

    cancelled_slots = 0
    for row in participants_to_remove:
        if row.slot_no is not None:
            cancelled_slots += 1
        db.delete(row)

    db.add(user_wallet)
    db.commit()

    refund_breakdown_text = _format_wallet_refund_breakdown(refund_distribution)

    try:
        if refund_amount > Decimal("0.00"):
            add_user_notification(
                db,
                current_user.id,
                "Tournament Cancelled",
                (
                    f"Entry cancelled for '{tournament.title}'. ₹{float(refund_amount):.2f} "
                    f"refunded (70%) to {refund_breakdown_text}."
                ),
                "TOURNAMENT",
            )
        else:
            add_user_notification(
                db,
                current_user.id,
                "Tournament Cancelled",
                f"Entry cancelled for '{tournament.title}'. No entry fee was deducted for your slot.",
                "TOURNAMENT",
            )

        if teammate_user_ids:
            for teammate_id in teammate_user_ids:
                add_user_notification(
                    db,
                    teammate_id,
                    "Team Entry Cancelled",
                    f"Your captain cancelled team entry for '{tournament.title}'.",
                    "TOURNAMENT",
                )
    except Exception:
        pass

    refund_value = float(refund_amount)
    return {
        "message": "Tournament entry cancelled successfully",
        "tournament_id": tournament_id,
        "cancelled_slots": cancelled_slots,
        "refund_percentage": 70,
        "refund_amount": refund_value,
        "refunded_to": "original_wallet_distribution" if refund_value > 0 else "none",
        "new_wallet_balance": float(get_total_balance(user_wallet)),
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

    # Optimization: Use joinedload('user') to avoid N+1 queries inside _build_slots_board
    from sqlalchemy.orm import joinedload
    participants = db.query(TournamentParticipant).options(
        joinedload(TournamentParticipant.user)
    ).filter(
        TournamentParticipant.tournament_id == tournament_id,
    ).all()

    my_participant = any(p.user_id == current_user.id for p in participants)
    is_clash_squad_tournament = "clash" in (tournament.game_name or "").lower()
    if current_user.role != "ADMIN" and is_clash_squad_tournament and not my_participant:
        raise HTTPException(status_code=403, detail="Join this tournament to view slot board")

    return _build_slots_board(tournament, participants, current_user_id=current_user.id)


@router.get("/my", response_model=List[TournamentResponse])
def get_my_tournaments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments)
):
    joined_subq = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(func.distinct(TournamentParticipant.slot_no)).label('j_count')
        )
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )

    rows = (
        db.query(Tournament, func.coalesce(joined_subq.c.j_count, 0))
        .join(TournamentParticipant, Tournament.id == TournamentParticipant.tournament_id)
        .outerjoin(joined_subq, Tournament.id == joined_subq.c.tournament_id)
        .filter(TournamentParticipant.user_id == current_user.id)
        .order_by(Tournament.match_time.desc())
        .all()
    )

    result = []
    for t, count in rows:
        if t.status != "LIVE":
            t.room_id = None
            t.room_password = None
        t.joined_count = count
        result.append(t)

    return result


@router.get("/{tournament_id}", response_model=TournamentResponse)
def get_tournament(
    tournament_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_tournaments)
):
    # Optimized: One query to get Tournament, Participant Count, and User Participation Status
    count_subq = (
        db.query(
            TournamentParticipant.tournament_id,
            func.count(func.distinct(TournamentParticipant.slot_no)).label('j_count')
        )
        .filter(TournamentParticipant.tournament_id == tournament_id)
        .group_by(TournamentParticipant.tournament_id)
        .subquery()
    )

    is_p_subq = (
        db.query(TournamentParticipant.tournament_id)
        .filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id == current_user.id
        )
        .exists()
    )

    result_row = (
        db.query(
            Tournament, 
            func.coalesce(count_subq.c.j_count, 0).label('total_joined'),
            is_p_subq.label('is_user_joined')
        )
        .outerjoin(count_subq, Tournament.id == count_subq.c.tournament_id)
        .filter(Tournament.id == tournament_id)
        .first()
    )

    if not result_row:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament, joined_count, is_participant = result_row
    
    # Secure room credentials
    if not is_participant or tournament.status != "LIVE":
        tournament.room_id       = None
        tournament.room_password = None

    tournament.joined_count = joined_count
    return tournament
