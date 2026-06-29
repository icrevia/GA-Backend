"""
Admin Ludo API — full control panel endpoints.
GET  /api/v1/admin/ludo/live          — active matches
GET  /api/v1/admin/ludo/history       — completed matches (paginated)
GET  /api/v1/admin/ludo/stats         — dashboard stats
GET  /api/v1/admin/ludo/config        — current config
PATCH /api/v1/admin/ludo/config       — update config
POST /api/v1/admin/ludo/{match_id}/force-end — admin force-end a match
GET  /api/v1/ludo/tiers               — PUBLIC: entry fee tiers for app
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, desc
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json

from core.database import get_db as get_async_db
from api.deps import get_current_active_admin, get_current_user
from models.user import User
from models.ludo import LudoMatch, LudoParticipant

router = APIRouter()

# ────────────────────────────────────────────────────────────
# In-memory config (persisted to DB via SystemConfig JSON field)
# ────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "is_enabled": True,
    "entry_fee_tiers": [
        {"fee": 0,  "prize": 0,   "label": "Practice", "color": "#6B7280"},
        {"fee": 5,  "prize": 9,   "label": "Starter",  "color": "#22C55E"},
        {"fee": 10, "prize": 18,  "label": "Classic",  "color": "#16A34A"},
        {"fee": 25, "prize": 45,  "label": "Pro",      "color": "#15803D"},
        {"fee": 50, "prize": 90,  "label": "Elite",    "color": "#166534"},
    ],
    "prize_multiplier": 1.8,
    "max_wait_seconds": 10,
    "turn_timer_seconds": 10,
    "match_duration_minutes": 7,
    "bot_enabled": True,
}

_ludo_config: dict = dict(_DEFAULT_CONFIG)


def _get_config_key():
    return "ludo_config"


async def _load_config_from_db(db: AsyncSession):
    """Load config from SystemConfig table if exists."""
    global _ludo_config
    try:
        from models.config import SystemConfig
        res = await db.execute(
            select(SystemConfig).where(SystemConfig.key == _get_config_key())
        )
        row = res.scalar_one_or_none()
        if row and row.value:
            stored = json.loads(row.value) if isinstance(row.value, str) else row.value
            _ludo_config = {**_DEFAULT_CONFIG, **stored}
    except Exception:
        pass  # Fallback to defaults if table doesn't exist yet


async def _save_config_to_db(db: AsyncSession, config: dict):
    """Persist config to SystemConfig table."""
    try:
        from models.config import SystemConfig
        res = await db.execute(
            select(SystemConfig).where(SystemConfig.key == _get_config_key())
        )
        row = res.scalar_one_or_none()
        if row:
            row.value = json.dumps(config)
        else:
            db.add(SystemConfig(key=_get_config_key(), value=json.dumps(config)))
        await db.commit()
    except Exception:
        pass


# ─────────────────────────────────────────
# PUBLIC endpoint — app fetches this on load
# ─────────────────────────────────────────

@router.get("/tiers")
async def get_ludo_tiers(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    """Returns entry fee tiers and game config for the app lobby."""
    await _load_config_from_db(db)
    return {
        "is_enabled": _ludo_config.get("is_enabled", True),
        "entry_fee_tiers": _ludo_config.get("entry_fee_tiers", _DEFAULT_CONFIG["entry_fee_tiers"]),
        "turn_timer_seconds": _ludo_config.get("turn_timer_seconds", 10),
        "match_duration_minutes": _ludo_config.get("match_duration_minutes", 7),
        "bot_enabled": _ludo_config.get("bot_enabled", True),
    }


# ─────────────────────────────────────────
# History endpoint (user-facing)
# ─────────────────────────────────────────

@router.get("/history")
async def get_ludo_history(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
    skip: int = 0,
    limit: int = 20,
):
    """Get the Ludo match history for the current user."""
    result = await db.execute(
        select(LudoMatch)
        .join(LudoParticipant, LudoParticipant.match_id == LudoMatch.id)
        .where(LudoParticipant.user_id == current_user.id)
        .order_by(LudoMatch.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    matches = result.scalars().all()

    out = []
    for m in matches:
        parts_res = await db.execute(
            select(LudoParticipant).where(LudoParticipant.match_id == m.id)
        )
        parts = parts_res.scalars().all()
        my_part = next((p for p in parts if p.user_id == current_user.id), None)
        out.append({
            "match_id": m.id,
            "entry_fee": float(m.entry_fee or 0),
            "prize_pool": float(m.prize_pool or 0),
            "status": m.status,
            "result": my_part.status if my_part else "UNKNOWN",
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out


# ─────────────────────────────────────────
# ADMIN endpoints
# ─────────────────────────────────────────

@router.get("/admin/live")
async def admin_get_live_ludo(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin: get all currently playing Ludo matches with full participant info."""
    result = await db.execute(
        select(LudoMatch)
        .where(LudoMatch.status == "PLAYING")
        .order_by(LudoMatch.created_at.desc())
    )
    matches = result.scalars().all()

    from services.ludo_orchestrator import orchestrator
    out = []
    for m in matches:
        parts_res = await db.execute(
            select(LudoParticipant).where(LudoParticipant.match_id == m.id)
        )
        parts = parts_res.scalars().all()

        # Enrich with username
        enriched_parts = []
        for p in parts:
            u_res = await db.execute(select(User.username, User.profile_pic).where(User.id == p.user_id))
            u_row = u_res.first()
            enriched_parts.append({
                "user_id": p.user_id,
                "username": u_row.username if u_row else f"User#{p.user_id}",
                "profile_pic": u_row.profile_pic if u_row else None,
                "color": p.color,
                "status": p.status,
                "is_bot": 99000 <= p.user_id <= 99999,
            })

        # Live engine state
        engine = orchestrator.games.get(m.id)
        engine_state = None
        if engine:
            engine_state = {
                "scores": dict(engine.scores),
                "current_turn": engine.get_current_player(),
                "remaining_seconds": max(0, (engine.end_time_ms - int(__import__('time').time() * 1000)) // 1000),
            }

        out.append({
            "match_id": m.id,
            "entry_fee": float(m.entry_fee or 0),
            "prize_pool": float(m.prize_pool or 0),
            "status": m.status,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "participants": enriched_parts,
            "engine_state": engine_state,
        })
    return out


@router.get("/admin/history")
async def admin_get_ludo_history(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
    skip: int = 0,
    limit: int = 50,
):
    """Admin: paginated completed Ludo matches."""
    result = await db.execute(
        select(LudoMatch)
        .where(LudoMatch.status.in_(["COMPLETED", "CANCELLED"]))
        .order_by(LudoMatch.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    matches = result.scalars().all()

    out = []
    for m in matches:
        parts_res = await db.execute(
            select(LudoParticipant).where(LudoParticipant.match_id == m.id)
        )
        parts = parts_res.scalars().all()

        enriched = []
        for p in parts:
            u_res = await db.execute(select(User.username).where(User.id == p.user_id))
            u_row = u_res.first()
            enriched.append({
                "user_id": p.user_id,
                "username": u_row.username if u_row else f"User#{p.user_id}",
                "color": p.color,
                "status": p.status,
                "is_bot": 99000 <= p.user_id <= 99999,
            })

        out.append({
            "match_id": m.id,
            "entry_fee": float(m.entry_fee or 0),
            "prize_pool": float(m.prize_pool or 0),
            "status": m.status,
            "winner_id": m.winner_id,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "participants": enriched,
        })
    return out


@router.get("/admin/stats")
async def admin_get_ludo_stats(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin: dashboard stats."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    live_count_res = await db.execute(
        select(func.count(LudoMatch.id)).where(LudoMatch.status == "PLAYING")
    )
    live_count = live_count_res.scalar() or 0

    today_count_res = await db.execute(
        select(func.count(LudoMatch.id)).where(LudoMatch.created_at >= today_start)
    )
    today_count = today_count_res.scalar() or 0

    total_prize_res = await db.execute(
        select(func.sum(LudoMatch.prize_pool)).where(LudoMatch.status == "COMPLETED")
    )
    total_prize = float(total_prize_res.scalar() or 0)

    total_entry_res = await db.execute(
        select(func.sum(LudoMatch.entry_fee)).where(LudoMatch.status == "COMPLETED")
    )
    total_entry = float(total_entry_res.scalar() or 0)

    # Matches in last 7 days by day
    from services.ludo_orchestrator import orchestrator
    pool_counts = {}
    try:
        from services.ludo_matchmaker import ludo_matchmaker
        for fee, pool in ludo_matchmaker.match_pools.items():
            pool_counts[str(fee)] = len(pool)
    except Exception:
        pass

    return {
        "live_matches": live_count,
        "today_matches": today_count,
        "total_prize_paid": total_prize,
        "total_entry_collected": total_entry,
        "platform_revenue": round(total_entry * 2 - total_prize, 2),
        "matchmaking_pools": pool_counts,
        "active_engines": len(orchestrator.games),
    }


@router.get("/admin/config")
async def admin_get_ludo_config(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin: get current Ludo config."""
    await _load_config_from_db(db)
    return _ludo_config


@router.patch("/admin/config")
async def admin_update_ludo_config(
    body: dict,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin: update Ludo config (partial update)."""
    global _ludo_config
    await _load_config_from_db(db)

    allowed_keys = {
        "is_enabled", "entry_fee_tiers", "prize_multiplier",
        "max_wait_seconds", "turn_timer_seconds",
        "match_duration_minutes", "bot_enabled",
    }
    for k, v in body.items():
        if k in allowed_keys:
            _ludo_config[k] = v

    await _save_config_to_db(db, _ludo_config)
    return {"status": "ok", "config": _ludo_config}


@router.post("/admin/{match_id}/force-end")
async def admin_force_end_match(
    match_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    """Admin: force-terminate a live match (no payout — declared void)."""
    from services.ludo_orchestrator import orchestrator

    if match_id not in orchestrator.games:
        raise HTTPException(status_code=404, detail="Match not live or not found")

    engine = orchestrator.games[match_id]
    engine.state = "COMPLETED"
    engine.winner = None

    await orchestrator._broadcast(match_id, engine)

    # Mark match as cancelled in DB (no payout)
    res = await db.execute(select(LudoMatch).where(LudoMatch.id == match_id))
    match = res.scalar_one_or_none()
    if match:
        match.status = "CANCELLED"
        await db.commit()

    # Full cleanup
    orchestrator._color_cache.pop(match_id, None)
    timer = orchestrator.timers.pop(match_id, None)
    if timer:
        timer.cancel()
    orchestrator.games.pop(match_id, None)

    return {"status": "cancelled", "match_id": match_id}
