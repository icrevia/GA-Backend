"""
Ludo Challenge Mode REST API
POST /api/v1/ludo/challenge/create         - Create challenge (deducts entry fee)
GET  /api/v1/ludo/challenge/list           - List OPEN challenges
GET  /api/v1/ludo/challenge/my             - My active challenges
POST /api/v1/ludo/challenge/{id}/join      - Join a challenge
POST /api/v1/ludo/challenge/{id}/play      - Tap Play Now (enter sync)
POST /api/v1/ludo/challenge/{id}/cancel    - Cancel own challenge (before opponent joins)
"""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from api.deps import get_current_user
from core.database import get_db as get_async_db
from models.ludo import LudoChallenge
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import (
    debit_wallet, credit_wallet,
    WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING,
    ZERO_MONEY, to_money, InsufficientWalletBalanceError,
)

router = APIRouter(prefix="/ludo/challenge", tags=["ludo-challenge"])

PRIZE_MULTIPLIER = Decimal("1.8")
CHALLENGE_TTL_HOURS = 1
SYNC_WINDOW_MINUTES = 10
ALLOWED_ENTRY_FEES = [10, 20, 50, 100]


class CreateChallengeRequest(BaseModel):
    entry_fee: int  # Must be one of ALLOWED_ENTRY_FEES


def _deductions_to_json(d: dict) -> dict:
    return {k: str(to_money(v)) for k, v in d.items()}


def _challenge_to_dict(c: LudoChallenge, me_id: int) -> dict:
    expires_in_s = max(0, int((c.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
    sync_deadline_iso = c.sync_deadline.isoformat() if c.sync_deadline else None
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
    if body.entry_fee not in ALLOWED_ENTRY_FEES:
        raise HTTPException(400, f"Entry fee must be one of {ALLOWED_ENTRY_FEES}")

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

    prize_pool = Decimal(body.entry_fee) * PRIZE_MULTIPLIER
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
            LudoChallenge.creator_id != current_user.id,
        ).order_by(LudoChallenge.created_at.desc()).limit(50)
    )
    challenges = res.scalars().all()
    # Load relationships manually (async-safe)
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

    challenge.opponent_id = current_user.id
    challenge.opponent_deductions = _deductions_to_json(deductions)
    challenge.status = "WAITING_SYNC"

    db.add(WalletTransaction(
        user_id=user.id,
        amount=-to_money(int(challenge.entry_fee)),
        transaction_type="LUDO_CHALLENGE_ENTRY",
        status="SUCCESS",
        reference_id=f"CHG-JOIN-{uuid.uuid4().hex[:8]}",
        remark=f"Joined Ludo Challenge #{challenge_id}",
    ))
    await db.commit()

    # Notify creator
    try:
        from core.websockets import manager
        await manager.send_personal_message({
            "type": "challenge_opponent_joined",
            "challenge_id": challenge_id,
            "opponent": {
                "user_id": current_user.id,
                "username": current_user.username,
                "profile_pic": current_user.profile_pic or "",
            },
        }, challenge.creator_id)
    except Exception:
        pass

    return {"success": True}


@router.post("/{challenge_id}/play")
async def enter_sync(
    challenge_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Tap Play Now — mark this player as synced. When both are synced, game launches."""
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    if challenge.status != "WAITING_SYNC":
        raise HTTPException(400, "Challenge is not in sync mode.")

    is_creator  = challenge.creator_id  == current_user.id
    is_opponent = challenge.opponent_id == current_user.id
    if not is_creator and not is_opponent:
        raise HTTPException(403, "You are not part of this challenge.")

    now = datetime.now(timezone.utc)

    # Set sync_deadline on FIRST Play Now tap
    if challenge.sync_deadline is None:
        challenge.sync_deadline = now + timedelta(minutes=SYNC_WINDOW_MINUTES)

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
            "sync_deadline": challenge.sync_deadline.isoformat(),
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
    challenge = await db.get(LudoChallenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "Challenge not found.")
    if challenge.creator_id != current_user.id:
        raise HTTPException(403, "Only the creator can cancel.")
    if challenge.status != "OPEN":
        raise HTTPException(400, "Can only cancel an OPEN challenge (before opponent joins).")

    challenge.status = "CANCELLED"
    user = await db.get(User, current_user.id)
    if user and challenge.creator_deductions:
        from services.ludo_challenge_manager import _parse_deductions, _refund_user
        deductions = _parse_deductions(challenge.creator_deductions)
        await _refund_user(db, user, deductions, Decimal("1.0"),
                           f"CHG-CANCEL-{challenge.id}", f"Challenge #{challenge.id} cancelled - full refund")
    await db.commit()
    return {"success": True, "message": "Challenge cancelled. Full refund issued."}
