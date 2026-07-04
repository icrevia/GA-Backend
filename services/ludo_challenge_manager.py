"""
ludo_challenge_manager.py
Background service for Ludo Challenge Mode.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from core.database import SessionLocal
from sqlalchemy.future import select
from models.ludo import LudoChallenge, LudoMatch, LudoParticipant
from models.user import User
from models.wallet import WalletTransaction
from services.wallet_balances import (
    credit_wallet,
    WALLET_BUCKET_BONUS, WALLET_BUCKET_DEPOSIT, WALLET_BUCKET_WINNING,
    ZERO_MONEY, to_money,
)

logger = logging.getLogger("GamerzAdda.LudoChallengeManager")

PRIZE_MULTIPLIER = Decimal("1.8")


def _parse_deductions(payload):
    buckets = {
        WALLET_BUCKET_BONUS: ZERO_MONEY,
        WALLET_BUCKET_DEPOSIT: ZERO_MONEY,
        WALLET_BUCKET_WINNING: ZERO_MONEY,
    }
    if not payload:
        return buckets
    for k in buckets:
        raw = payload.get(k)
        if raw is not None:
            try:
                buckets[k] = to_money(raw)
            except Exception:
                pass
    return buckets


async def _refund_user(db, user, deductions, rate, ref_prefix, remark):
    total = ZERO_MONEY
    for bucket, amount in deductions.items():
        refund_amt = to_money(amount * rate)
        if refund_amt > ZERO_MONEY:
            credit_wallet(user, refund_amt, bucket)
            total += refund_amt
    db.add(WalletTransaction(
        user_id=user.id,
        amount=total,
        transaction_type="LUDO_CHALLENGE_REFUND",
        status="SUCCESS",
        reference_id=f"{ref_prefix}-{uuid.uuid4().hex[:8]}",
        remark=remark,
    ))
    return total


async def expire_challenges():
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        res = await db.execute(
            select(LudoChallenge).where(
                LudoChallenge.status == "OPEN",
                LudoChallenge.expires_at <= now,
            )
        )
        expired = res.scalars().all()
        if not expired:
            return

        for challenge in expired:
            challenge.status = "EXPIRED"
            creator = await db.get(User, challenge.creator_id)
            if creator and challenge.creator_deductions:
                deductions = _parse_deductions(challenge.creator_deductions)
                await _refund_user(
                    db, creator, deductions, Decimal("1.0"),
                    f"CHG-EXP-{challenge.id}",
                    f"Challenge #{challenge.id} expired - full refund",
                )
                logger.info("Challenge %d expired - refunded creator %d", challenge.id, challenge.creator_id)
            try:
                from core.websockets import manager
                await manager.send_personal_message({
                    "type": "challenge_expired",
                    "challenge_id": challenge.id,
                    "message": "Your challenge expired. Full refund issued.",
                }, challenge.creator_id)
            except Exception:
                pass

        await db.commit()


async def handle_sync_timeouts():
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        res = await db.execute(
            select(LudoChallenge).where(
                LudoChallenge.status == "WAITING_SYNC",
                LudoChallenge.sync_deadline <= now,
            )
        )
        timed_out = res.scalars().all()
        if not timed_out:
            return

        for challenge in timed_out:
            challenge.status = "CANCELLED"
            creator_late  = not challenge.creator_synced
            opponent_late = not challenge.opponent_synced

            creator  = await db.get(User, challenge.creator_id)
            opponent = await db.get(User, challenge.opponent_id) if challenge.opponent_id else None

            creator_rate  = Decimal("0.7") if creator_late  else Decimal("1.0")
            opponent_rate = Decimal("0.7") if opponent_late else Decimal("1.0")

            if creator and challenge.creator_deductions:
                await _refund_user(
                    db, creator,
                    _parse_deductions(challenge.creator_deductions),
                    creator_rate,
                    f"CHG-STO-{challenge.id}-C",
                    f"Challenge #{challenge.id} sync timeout - {'70%' if creator_late else '100%'} refund",
                )

            if opponent and challenge.opponent_deductions:
                await _refund_user(
                    db, opponent,
                    _parse_deductions(challenge.opponent_deductions),
                    opponent_rate,
                    f"CHG-STO-{challenge.id}-O",
                    f"Challenge #{challenge.id} sync timeout - {'70%' if opponent_late else '100%'} refund",
                )

            logger.info("Challenge %d sync timeout. creator_late=%s opponent_late=%s",
                        challenge.id, creator_late, opponent_late)

            try:
                from core.websockets import manager
                msg = {"type": "challenge_cancelled", "challenge_id": challenge.id, "reason": "sync_timeout"}
                if challenge.creator_id:
                    await manager.send_personal_message(msg, challenge.creator_id)
                if challenge.opponent_id:
                    await manager.send_personal_message(msg, challenge.opponent_id)
            except Exception:
                pass

        await db.commit()


async def launch_game(challenge_id: int):
    import random
    from services.ludo_orchestrator import orchestrator

    async with SessionLocal() as db:
        challenge = await db.get(LudoChallenge, challenge_id)
        if not challenge or challenge.status != "WAITING_SYNC":
            return
        if not (challenge.creator_synced and challenge.opponent_synced):
            return

        prize_pool = challenge.entry_fee * PRIZE_MULTIPLIER
        match = LudoMatch(entry_fee=challenge.entry_fee, prize_pool=prize_pool, status="PLAYING")
        db.add(match)
        await db.flush()

        pairs = [("RED", "YELLOW"), ("GREEN", "BLUE")]
        color1, color2 = random.choice(pairs)
        if random.random() < 0.5:
            color1, color2 = color2, color1

        p1 = LudoParticipant(match_id=match.id, user_id=challenge.creator_id,  color=color1)
        p2 = LudoParticipant(match_id=match.id, user_id=challenge.opponent_id, color=color2)
        db.add_all([p1, p2])

        challenge.match_id = match.id
        challenge.status   = "PLAYING"
        await db.commit()
        match_id  = match.id
        c_id      = challenge.creator_id
        o_id      = challenge.opponent_id

    await orchestrator.start_game(match_id)

    async with SessionLocal() as db:
        creator  = await db.get(User, c_id)
        opponent = await db.get(User, o_id)

    from core.websockets import manager
    await manager.send_personal_message({
        "type": "challenge_started", "challenge_id": challenge_id, "match_id": match_id,
        "your_color": color1,
        "opponent": {"user_id": o_id,
                     "username": opponent.username if opponent else "Opponent",
                     "profile_pic": (opponent.profile_pic or "") if opponent else ""},
    }, c_id)

    await manager.send_personal_message({
        "type": "challenge_started", "challenge_id": challenge_id, "match_id": match_id,
        "your_color": color2,
        "opponent": {"user_id": c_id,
                     "username": creator.username if creator else "Creator",
                     "profile_pic": (creator.profile_pic or "") if creator else ""},
    }, o_id)

    logger.info("Challenge %d started as match %d", challenge_id, match_id)


async def _expire_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await expire_challenges()
        except Exception as e:
            logger.error("expire_loop error: %s", e)


async def _sync_timeout_loop():
    while True:
        await asyncio.sleep(10)
        try:
            await handle_sync_timeouts()
        except Exception as e:
            logger.error("sync_timeout_loop error: %s", e)


def start_background_tasks():
    asyncio.create_task(_expire_loop(),       name="challenge_expire_loop")
    asyncio.create_task(_sync_timeout_loop(), name="challenge_sync_loop")
