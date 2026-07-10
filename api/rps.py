from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, desc
from typing import List
from datetime import datetime, timezone

from core.database import get_db as get_async_db
from api.deps import get_current_active_admin, get_current_user
from models.user import User
from models.rps import RPSMatch, RPSParticipant

router = APIRouter()

_DEFAULT_CONFIG = {
    "is_enabled": True,
    "entry_fee_tiers": [10, 20, 50, 100, 200, 500, 1000],
    "prize_multiplier": 1.8,
    "turn_timer_seconds": 10,
    "draw_refund_percentage": 100
}
_rps_config = dict(_DEFAULT_CONFIG)

def _get_config_key():
    return "rps_config"

async def _load_config_from_db(db: AsyncSession):
    import json
    try:
        from models.config import SystemConfig
        res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == _get_config_key()))
        row = res.scalar_one_or_none()
        if row and row.config_value:
            global _rps_config
            stored = json.loads(row.config_value) if isinstance(row.config_value, str) else row.config_value
            _rps_config = {**_DEFAULT_CONFIG, **stored}
    except Exception:
        pass

@router.get("/tiers")
async def get_rps_tiers(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
):
    await _load_config_from_db(db)
    return {
        "is_enabled": _rps_config.get("is_enabled", True),
        "tiers": _rps_config.get("entry_fee_tiers", _DEFAULT_CONFIG["entry_fee_tiers"]),
        "turn_timer_seconds": _rps_config.get("turn_timer_seconds", _DEFAULT_CONFIG["turn_timer_seconds"]),
        "prize_multiplier": _rps_config.get("prize_multiplier", _DEFAULT_CONFIG["prize_multiplier"])
    }

@router.get("/history")
async def get_rps_history(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_user),
    skip: int = 0,
    limit: int = 20,
):
    result = await db.execute(
        select(RPSMatch)
        .join(RPSParticipant, RPSParticipant.match_id == RPSMatch.id)
        .where(RPSParticipant.user_id == current_user.id)
        .order_by(RPSMatch.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    matches = result.scalars().all()
    out = []
    for m in matches:
        parts_res = await db.execute(
            select(RPSParticipant, User)
            .join(User, User.id == RPSParticipant.user_id)
            .where(RPSParticipant.match_id == m.id)
        )
        parts_data = parts_res.all()
        
        my_part = None
        opp_part = None
        opp_user = None
        
        for p, u in parts_data:
            if p.user_id == current_user.id:
                my_part = p
            else:
                opp_part = p
                opp_user = u
                
        money_won = 0.0
        if my_part and my_part.status == "WON":
            money_won = float(m.prize_pool or 0)
        elif my_part and my_part.status == "DRAW":
            money_won = float(m.entry_fee or 0) # Refunded
            
        out.append({
            "match_id": m.id,
            "entry_fee": float(m.entry_fee or 0),
            "prize_pool": float(m.prize_pool or 0),
            "status": m.status,
            "result": my_part.status if my_part else "UNKNOWN",
            "my_move": my_part.move if my_part else None,
            "opponent_move": opp_part.move if opp_part else None,
            "opponent_name": opp_user.username if opp_user else "Unknown",
            "opponent_pic": opp_user.profile_pic if opp_user else None,
            "money_won": money_won,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out

@router.get("/admin/live")
async def admin_get_live_rps(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    result = await db.execute(
        select(RPSMatch)
        .where(RPSMatch.status == "PLAYING")
        .order_by(RPSMatch.created_at.desc())
    )
    matches = result.scalars().all()

    from services.rps_orchestrator import orchestrator
    out = []
    for m in matches:
        parts_res = await db.execute(select(RPSParticipant).where(RPSParticipant.match_id == m.id))
        parts = parts_res.scalars().all()

        enriched_parts = []
        for p in parts:
            u_res = await db.execute(select(User.username).where(User.id == p.user_id))
            u_row = u_res.first()
            enriched_parts.append({
                "user_id": p.user_id,
                "username": u_row.username if u_row else f"User#{p.user_id}",
                "move": p.move,
                "status": p.status
            })

        engine = orchestrator.games.get(m.id)
        engine_state = engine.get_state() if engine else None

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


@router.get('/admin/history')
async def admin_get_history(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
    limit: int = 30
):
    result = await db.execute(
        select(RPSMatch)
        .where(RPSMatch.status.in_(['COMPLETED', 'CANCELLED']))
        .order_by(RPSMatch.created_at.desc())
        .limit(limit)
    )
    matches = result.scalars().all()
    
    out = []
    for m in matches:
        parts_res = await db.execute(select(RPSParticipant, User).join(User, User.id == RPSParticipant.user_id).where(RPSParticipant.match_id == m.id))
        parts_data = parts_res.all()
        enriched_parts = []
        for p, u in parts_data:
            enriched_parts.append({
                'user_id': p.user_id,
                'username': u.username,
                'move': p.move,
                'status': p.status
            })
        out.append({
            'match_id': m.id,
            'entry_fee': float(m.entry_fee or 0),
            'prize_pool': float(m.prize_pool or 0),
            'status': m.status,
            'winner_id': m.winner_id,
            'created_at': m.created_at.isoformat() if m.created_at else None,
            'participants': enriched_parts,
        })
    return out

@router.get('/admin/stats')
async def admin_get_stats(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    # live matches
    res_live = await db.execute(select(func.count(RPSMatch.id)).where(RPSMatch.status == 'PLAYING'))
    live_count = res_live.scalar() or 0
    
    # today matches
    from datetime import datetime, time
    today = datetime.combine(datetime.now(), time.min)
    res_today = await db.execute(select(func.count(RPSMatch.id)).where(RPSMatch.created_at >= today))
    today_count = res_today.scalar() or 0
    
    res_prize = await db.execute(select(func.sum(RPSMatch.prize_pool)).where(RPSMatch.status == 'COMPLETED'))
    total_prize = float(res_prize.scalar() or 0)
    
    res_entry = await db.execute(select(func.sum(RPSMatch.entry_fee)).where(RPSMatch.status == 'COMPLETED'))
    total_entry = float(res_entry.scalar() or 0)
    
    platform_revenue = max(0, total_entry - total_prize)
    
    from services.rps_matchmaker import matchmaker
    pools = {str(fee): len(q) for fee, q in matchmaker.queues.items()}
    
    from services.rps_orchestrator import orchestrator
    
    return {
        'live_matches': live_count,
        'today_matches': today_count,
        'total_prize_paid': total_prize,
        'total_entry_collected': total_entry,
        'platform_revenue': platform_revenue,
        'matchmaking_pools': pools,
        'active_engines': len(orchestrator.games)
    }

@router.get('/admin/config')
async def admin_get_config(
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    await _load_config_from_db(db)
    return _rps_config

@router.patch('/admin/config')
async def admin_update_config(
    data: dict,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    global _rps_config
    import json
    from models.config import SystemConfig
    await _load_config_from_db(db)
    new_cfg = {**_rps_config, **data}
    
    res = await db.execute(select(SystemConfig).where(SystemConfig.config_key == _get_config_key()))
    row = res.scalar_one_or_none()
    if row:
        row.config_value = json.dumps(new_cfg)
    else:
        row = SystemConfig(config_key=_get_config_key(), config_value=json.dumps(new_cfg))
        db.add(row)
    await db.commit()
    
    _rps_config = new_cfg
    return {'status': 'success'}

@router.post('/admin/{match_id}/force-end')
async def admin_force_end(
    match_id: int,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(get_current_active_admin),
):
    from services.rps_orchestrator import orchestrator
    orchestrator.remove_game(match_id)
    
    result = await db.execute(select(RPSMatch).where(RPSMatch.id == match_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail='Match not found')
        
    if m.status == 'PLAYING':
        m.status = 'CANCELLED'
        # refund logic can be added here if needed
        await db.commit()
    return {'status': 'success'}
