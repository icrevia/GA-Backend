"""
Ludo Challenge Mode REST API
POST /api/v1/ludo/challenge/create         - Create challenge (deducts entry fee)
GET  /api/v1/ludo/challenge/list           - List OPEN challenges
GET  /api/v1/ludo/challenge/my             - My active challenges
GET  /api/v1/ludo/challenge/{id}           - Single challenge detail
POST /api/v1/ludo/challenge/{id}/join      - Join a challenge
POST /api/v1/ludo/challenge/{id}/play      - Tap Play Now (enter sync)
POST /api/v1/ludo/challenge/{id}/cancel    - Cancel own challenge (before opponent joins)
"""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.deps import get_current_user, get_current_active_admin
from core.database import get_db as get_async_db
from models.ludo import LudoChallenge
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import (
    debit_wallet, credit_wallet,
    WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING,
    ZERO_MONEY, to_money, InsufficientWalletBalanceError,
)
from sqlalchemy import func, desc

router = APIRouter(prefix="/challenge", tags=["ludo-challenge"])

CHALLENGE_TTL_HOURS = 1
CHALLENGE_TTL_HOURS = 1
SYNC_WINDOW_MINUTES = 10


class CreateChallengeRequest(BaseModel):
    entry_fee: int  # Must be >= 10


def _deductions_to_json(d: dict) -> dict:
    return {k: str(to_money(v)) for k, v in d.items()}


def _challenge_to_dict(c: LudoChallenge, me_id: int) -> dict:
    expires_in_s = max(0, int((c.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
    sync_deadline_iso = c.sync_deadline.isoformat() if c.sync_deadline else None
    sync_seconds_left = None
    if c.sync_deadline:
        sync_seconds_left = max(0, int((c.sync_deadline.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
    return {
        "id": c.id,
        "creator_id": c.creator_id,
        "creator_name": c.creator.username if c.creator else "",
        "creator_pic": (c.creator.profile_pic or "") if c.creator else "",
        "opponent_id": c.opponent_id,
        "opponent_name": c.opponent.username if (c.opponent_id and c.opponent) else None,
        "opponent_pic": (c.opponent.profile_pic or "") if (c.opponent_id and c.opponent) else None,
        "entry_fee": float(c.entry_fee),
        "prize_pool": float(c.prize_pool),
        "status": c.status,
        "expires_in_seconds": expires_in_s,
        "sync_deadline": sync_deadline_iso,
        "sync_seconds_left": sync_seconds_left,
        "creator_synced": c.creator_synced,
        "opponent_synced": c.opponent_synced,
        "match_id": c.match_id,
        "is_mine": c.creator_id == me_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.post("/create")
async def create_challenge(
    body: CreateChallengeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    if body.entry_fee < 10:
        raise HTTPException(400, "Entry fee must be at least ₹10")

    # Max 1 active challenge per user
    existing = await db.execute(
        select(LudoChallenge).where(
            LudoChallenge.creator_id == current_user.id,
            LudoChallenge.status.in_(["OPEN", "WAITING_SYNC", "PLAYING"]),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "You already have an active challenge. Cancel it first.")

    user = await db.get(User, current_user.id)
    try:
        deductions = debit_wallet(user, body.entry_fee, spend_order=(WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING))
    except InsufficientWalletBalanceError:
        raise HTTPException(400, "Insufficient balance.")

    # Get platform fee percentage (default 10%)
    fee_res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == "LUDO_CHALLENGE_FEE_PERCENT"))
    fee_cfg = fee_res.scalar_one_or_none()
    fee_percent = Decimal("10.0")
    if fee_cfg and fee_cfg.config_value:
        try:
            fee_percent = Decimal(fee_cfg.config_value)
        except Exception:
            pass
            
    prize_pool = (Decimal(body.entry_fee) * Decimal("2.0") * (Decimal("100.0") - fee_percent)) / Decimal("100.0")
    now = datetime.now(timezone.utc)
    challenge = LudoChallenge(
        creator_id=current_user.id,
        creator_deductions=_deductions_to_json(deductions),
        entry_fee=Decimal(body.entry_fee),
        prize_pool=prize_pool,
        expires_at=now + timedelta(hours=CHALLENGE_TTL_HOURS),
        status="OPEN",
    )
    db.add(challenge)
    db.add(WalletTransaction(
        user_id=user.id,
        amount=-to_money(body.entry_fee),
        transaction_type="LUDO_CHALLENGE_ENTRY",
        status="SUCCESS",
        reference_id=f"CHG-ENTRY-{uuid.uuid4().hex[:8]}",
        remark="Ludo Challenge entry fee",
    ))
    await db.commit()
    await db.refresh(challenge)
    return {"success": True, "challenge_id": challenge.id}


@router.get("/list")
async def list_challenges(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Return all OPEN challenges excluding user's own."""
    now = datetime.now(timezone.utc)
    res = await db.execute(
        select(LudoChallenge).where(
            LudoChallenge.status == "OPEN",
            LudoChallenge.expires_at > now,
        ).order_by(LudoChallenge.created_at.desc()).limit(50)
    )
    challenges = res.scalars().all()
    result = []
    for c in challenges:
        await db.refresh(c, ["creator"])
        result.append(_challenge_to_dict(c, current_user.id))
    return result


@router.get("/my")
async def my_challenges(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Return challenges created by or joined by the current user."""
    res = await db.execute(
        select(LudoChallenge).where(
            (LudoChallenge.creator_id == current_user.id) |
            (LudoChallenge.opponent_id == current_user.id),
            LudoChallenge.status.in_(["OPEN", "WAITING_SYNC", "PLAYING"]),
        ).order_by(LudoChallenge.created_at.desc())
    )
    challenges = res.scalars().all()
    result = []
    for c in challenges:
        await db.refresh(c, ["creator", "opponent"])
        result.append(_challenge_to_dict(c, current_user.id))
    return result


@router.get("/{challenge_id}")
async def get_challenge(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Get a single challenge by ID."""
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    # Only participants can view a non-OPEN challenge
    if challenge.status != "OPEN" and challenge.creator_id != current_user.id and challenge.opponent_id != current_user.id:
        raise HTTPException(403, "You are not part of this challenge.")
    await db.refresh(challenge, ["creator", "opponent"])
    return _challenge_to_dict(challenge, current_user.id)


@router.post("/{challenge_id}/join")
async def join_challenge(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    if challenge.status != "OPEN":
        raise HTTPException(400, "Challenge is no longer open.")
    if challenge.creator_id == current_user.id:
        raise HTTPException(400, "You cannot join your own challenge.")
    if challenge.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(400, "Challenge has expired.")

    # Check user has no other active challenge
    existing = await db.execute(
        select(LudoChallenge).where(
            (LudoChallenge.opponent_id == current_user.id) |
            (LudoChallenge.creator_id == current_user.id),
            LudoChallenge.status.in_(["OPEN", "WAITING_SYNC", "PLAYING"]),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "You already have an active challenge.")

    user = await db.get(User, current_user.id)
    try:
        deductions = debit_wallet(user, int(challenge.entry_fee), spend_order=(WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING))
    except InsufficientWalletBalanceError:
        raise HTTPException(400, "Insufficient balance.")

    now = datetime.now(timezone.utc)
    challenge.opponent_id = current_user.id
    challenge.opponent_deductions = _deductions_to_json(deductions)
    challenge.status = "WAITING_SYNC"
    # Set sync_deadline immediately so the timeout loop always fires,
    # even if neither player ever presses Play Now.
    challenge.sync_deadline = now + timedelta(minutes=SYNC_WINDOW_MINUTES)

    db.add(WalletTransaction(
        user_id=user.id,
        amount=-to_money(int(challenge.entry_fee)),
        transaction_type="LUDO_CHALLENGE_ENTRY",
        status="SUCCESS",
        reference_id=f"CHG-JOIN-{uuid.uuid4().hex[:8]}",
        remark=f"Joined Ludo Challenge #{challenge_id}",
    ))
    await db.commit()

    # Notify creator — include full sync deadline so app can show the countdown
    try:
        from core.websockets import manager
        await manager.send_personal_message({
            "type": "challenge_opponent_joined",
            "challenge_id": challenge_id,
            "sync_deadline": challenge.sync_deadline.isoformat(),
            "opponent": {
                "user_id": current_user.id,
                "username": current_user.username,
                "profile_pic": current_user.profile_pic or "",
            },
        }, challenge.creator_id)
        
        # Send Push Notification to Creator
        creator = await db.get(User, challenge.creator_id)
        if creator and getattr(creator, "fcm_token", None):
            from services.push_notifications import send_push
            send_push(
                fcm_token=creator.fcm_token,
                title="Challenge Accepted! ⚔️",
                body=f"{current_user.username} has joined your Ludo Challenge. Tap to sync and start the match. If you don't join within 10 minutes, 30% of your entry amount will be deducted as a platform fee.",
                data={"type": "LUDO_CHALLENGE", "challenge_id": str(challenge_id)}
            )
    except Exception as e:
        import logging
        logging.error(f"Failed to send challenge joined notifications: {e}")

    return {"success": True, "sync_deadline": challenge.sync_deadline.isoformat()}


@router.post("/{challenge_id}/play")
async def enter_sync(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Tap Play Now - mark this player as synced. When both are synced, game launches."""
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    if challenge.status != "WAITING_SYNC":
        raise HTTPException(400, "Challenge is not in sync mode.")

    is_creator  = challenge.creator_id  == current_user.id
    is_opponent = challenge.opponent_id == current_user.id
    if not is_creator and not is_opponent:
        raise HTTPException(403, "You are not part of this challenge.")

    # Guard against already-synced players tapping again
    if is_creator and challenge.creator_synced:
        return {"success": True, "waiting_for_opponent": not challenge.opponent_synced}
    if is_opponent and challenge.opponent_synced:
        return {"success": True, "waiting_for_opponent": not challenge.creator_synced}

    if is_creator:
        challenge.creator_synced = True
    else:
        challenge.opponent_synced = True

    await db.commit()

    # Notify the other player
    try:
        from core.websockets import manager
        other_id = challenge.opponent_id if is_creator else challenge.creator_id
        await manager.send_personal_message({
            "type": "challenge_sync_request",
            "challenge_id": challenge_id,
            "from_user_id": current_user.id,
            "sync_deadline": challenge.sync_deadline.isoformat() if challenge.sync_deadline else None,
        }, other_id)
    except Exception:
        pass

    # Launch if both synced
    if challenge.creator_synced and challenge.opponent_synced:
        from services.ludo_challenge_manager import launch_game
        import asyncio
        asyncio.create_task(launch_game(challenge_id), name=f"launch_challenge_{challenge_id}")

    return {"success": True, "waiting_for_opponent": not (challenge.creator_synced and challenge.opponent_synced)}


@router.post("/{challenge_id}/cancel")
async def cancel_challenge(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Cancel / abandon a challenge:
    - OPEN        → 100% refund to creator
    - WAITING_SYNC → 70% refund to BOTH players (30% platform penalty)
    - PLAYING      → not allowed
    """
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    if current_user.id not in (challenge.creator_id, challenge.opponent_id):
        raise HTTPException(403, "Only participants can cancel this challenge.")
    if challenge.status == "OPEN" and challenge.creator_id != current_user.id:
        raise HTTPException(403, "Only the creator can cancel an OPEN challenge.")
    if challenge.status not in ("OPEN", "WAITING_SYNC"):
        raise HTTPException(400, "Cannot cancel a challenge that is already in progress or completed.")

    from services.ludo_challenge_manager import _parse_deductions, _refund_user

    original_status = challenge.status
    challenge.status = "CANCELLED"

    if original_status == "OPEN":
        # 100% refund to creator since no one joined yet
        creator = await db.get(User, current_user.id)
        if creator and challenge.creator_deductions:
            await _refund_user(
                db, creator,
                _parse_deductions(challenge.creator_deductions),
                Decimal("1.0"),
                f"CHG-CANCEL-{challenge.id}",
                f"Challenge #{challenge.id} cancelled - 100% refund"
            )
        await db.commit()
        return {"success": True, "message": "Challenge cancelled. Full refund issued.", "refund_type": "FULL_100"}

    else:
        # WAITING_SYNC: 70% refund to abandoner, 100% to other player
        creator  = await db.get(User, challenge.creator_id)
        opponent = await db.get(User, challenge.opponent_id) if challenge.opponent_id else None

        abandoner_name = current_user.username
        creator_rate = Decimal("0.7") if current_user.id == challenge.creator_id else Decimal("1.0")
        opponent_rate = Decimal("0.7") if current_user.id == challenge.opponent_id else Decimal("1.0")

        if creator and challenge.creator_deductions:
            await _refund_user(
                db, creator,
                _parse_deductions(challenge.creator_deductions),
                creator_rate,
                f"CHG-ABN-{challenge.id}-C",
                f"Challenge #{challenge.id} cancelled by {abandoner_name} - {int(creator_rate * 100)}% refund"
            )

        if opponent and challenge.opponent_deductions:
            await _refund_user(
                db, opponent,
                _parse_deductions(challenge.opponent_deductions),
                opponent_rate,
                f"CHG-ABN-{challenge.id}-O",
                f"Challenge #{challenge.id} cancelled by {abandoner_name} - {int(opponent_rate * 100)}% refund"
            )

        await db.commit()

        # Notify other player that challenge was abandoned
        other_player_id = challenge.opponent_id if current_user.id == challenge.creator_id else challenge.creator_id
        if other_player_id:
            try:
                from core.websockets import manager
                other_rate_pct = 70 if other_player_id == current_user.id else 100
                await manager.send_personal_message({
                    "type": "challenge_cancelled",
                    "challenge_id": challenge_id,
                    "reason": "abandoned",
                    "message": f"The challenge was cancelled by {abandoner_name}. You received a {other_rate_pct}% refund.",
                }, other_player_id)
            except Exception:
                pass

        return {
            "success": True,
            "message": "Challenge cancelled. You received a 70% refund.",
            "refund_type": "MIXED"
        }

# ─── Admin Routes ─────────────────────────────────────────────────────────────

@router.get("/admin/stats")
async def get_challenge_admin_stats(
    admin: User = Depends(get_current_active_admin),
    db: AsyncSession = Depends(get_async_db),
):
    live_res = await db.execute(
        select(func.count(LudoChallenge.id)).where(LudoChallenge.status.in_(["OPEN", "WAITING_SYNC", "PLAYING"]))
    )
    live_count = live_res.scalar() or 0

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_res = await db.execute(
        select(func.count(LudoChallenge.id)).where(LudoChallenge.created_at >= today)
    )
    today_count = today_res.scalar() or 0

    total_pool_res = await db.execute(
        select(func.sum(LudoChallenge.prize_pool)).where(LudoChallenge.status == "COMPLETED")
    )
    total_paid = float(total_pool_res.scalar() or 0)

    total_entry_res = await db.execute(
        select(func.sum(LudoChallenge.entry_fee)).where(LudoChallenge.status.in_(["COMPLETED", "PLAYING"]))
    )
    total_entry = float(total_entry_res.scalar() or 0) * 2

    revenue = total_entry - total_paid

    return {
        "live_matches": live_count,
        "today_matches": today_count,
        "total_prize_paid": total_paid,
        "total_entry_collected": total_entry,
        "platform_revenue": revenue,
    }


@router.get("/admin/live")
async def get_challenge_admin_live(
    admin: User = Depends(get_current_active_admin),
    db: AsyncSession = Depends(get_async_db),
):
    res = await db.execute(
        select(LudoChallenge)
        .where(LudoChallenge.status.in_(["OPEN", "WAITING_SYNC", "PLAYING"]))
        .order_by(desc(LudoChallenge.created_at))
    )
    challenges = res.scalars().all()
    out = []
    for c in challenges:
        await db.refresh(c, ["creator", "opponent"])
        out.append(_challenge_to_dict(c, 0))
    return out


@router.get("/admin/history")
async def get_challenge_admin_history(
    limit: int = 50,
    admin: User = Depends(get_current_active_admin),
    db: AsyncSession = Depends(get_async_db),
):
    res = await db.execute(
        select(LudoChallenge)
        .where(LudoChallenge.status.in_(["COMPLETED", "CANCELLED", "EXPIRED"]))
        .order_by(desc(LudoChallenge.created_at))
        .limit(limit)
    )
    challenges = res.scalars().all()
    out = []
    for c in challenges:
        await db.refresh(c, ["creator", "opponent"])
        out.append(_challenge_to_dict(c, 0))
    return out


@router.post("/admin/{challenge_id}/force-cancel")
async def force_cancel_challenge(
    challenge_id: int,
    admin: User = Depends(get_current_active_admin),
    db: AsyncSession = Depends(get_async_db),
):
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ["OPEN", "WAITING_SYNC", "PLAYING"]:
        raise HTTPException(400, f"Cannot cancel challenge in state {challenge.status}")

    # Refund creator
    await db.refresh(challenge, ["creator", "opponent"])
    if challenge.creator_id:
        try:
            creator_user = await db.get(User, challenge.creator_id)
            if creator_user:
                credit_wallet(creator_user, challenge.entry_fee, WALLET_BUCKET_DEPOSIT)
                db.add(WalletTransaction(
                    user_id=creator_user.id,
                    amount=to_money(challenge.entry_fee),
                    transaction_type="LUDO_CHALLENGE_REFUND",
                    status="SUCCESS",
                    reference_id=f"CHG-RF-ADM-{uuid.uuid4().hex[:8]}",
                    remark="Admin forced cancel refund"
                ))
        except Exception:
            pass

    # Refund opponent if joined
    if challenge.opponent_id:
        try:
            opponent_user = await db.get(User, challenge.opponent_id)
            if opponent_user:
                credit_wallet(opponent_user, challenge.entry_fee, WALLET_BUCKET_DEPOSIT)
                db.add(WalletTransaction(
                    user_id=opponent_user.id,
                    amount=to_money(challenge.entry_fee),
                    transaction_type="LUDO_CHALLENGE_REFUND",
                    status="SUCCESS",
                    reference_id=f"CHG-RF-ADM-{uuid.uuid4().hex[:8]}",
                    remark="Admin forced cancel refund"
                ))
        except Exception:
            pass

    challenge.status = "CANCELLED"
    await db.commit()
    return {"success": True, "message": "Challenge force cancelled and refunded."}
