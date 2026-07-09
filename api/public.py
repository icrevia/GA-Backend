from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from core.database import get_db_sync as get_db
from models.participant import TournamentParticipant
from models.tournament import Tournament
from models.user import User
from models.wallet import WalletTransaction
from services.match_stats import normalize_leaderboard_category, leaderboard_prize_payment_mode

router = APIRouter()


_LEADERBOARD_TIME_RANGES = {
    "today": "today",
    "last_7_days": "last_7_days",
    "last7": "last_7_days",
    "7d": "last_7_days",
    "last_30_days": "last_30_days",
    "last30": "last_30_days",
    "30d": "last_30_days",
    "lifetime": "lifetime",
    "all_time": "lifetime",
    "all": "lifetime",
}


def _normalize_leaderboard_time_range(raw: str | None) -> str | None:
    if not raw:
        return None
    clean = "_".join(raw.strip().lower().replace("-", "_").split())
    return _LEADERBOARD_TIME_RANGES.get(clean)


def _leaderboard_range_start(now_utc: datetime, time_range: str) -> datetime | None:
    if time_range == "lifetime":
        return None
    if time_range == "today":
        return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if time_range == "last_7_days":
        return now_utc - timedelta(days=7)
    if time_range == "last_30_days":
        return now_utc - timedelta(days=30)
    return None


def _mask_username(username: str | None) -> str:
    if not username:
        return "Player"
    clean = " ".join(username.strip().split())
    if not clean:
        return "Player"
    parts = clean.split(" ")
    if len(parts) >= 2 and parts[-1]:
        return f"{parts[0]} {parts[-1][0].upper()}."
    if len(clean) <= 2:
        return f"{clean[0].upper()}*"
    return f"{clean[:2]}***"


@router.get("/leaderboard")
def get_public_leaderboard(
    db: Session = Depends(get_db),
    category: str = Query(default="free_fire"),
    time_range: str = Query(default="lifetime"),
    limit: int = Query(default=3, ge=1, le=5),
):
    normalized_category = normalize_leaderboard_category(category)
    if not normalized_category:
        raise HTTPException(
            status_code=400,
            detail="Invalid category. Use one of: free_fire, free_fire_max, clash_squad, ludo, quiz",
        )

    normalized_time_range = _normalize_leaderboard_time_range(time_range)
    if not normalized_time_range:
        raise HTTPException(
            status_code=400,
            detail="Invalid time_range. Use one of: today, last_7_days, last_30_days, lifetime",
        )

    now_utc = datetime.now(timezone.utc)
    range_start = _leaderboard_range_start(now_utc, normalized_time_range)
    safe_limit = max(1, min(limit, 5))

    # ── Ludo ──────────────────────────────────────────────────────────────────
    if normalized_category == "ludo":
        from models.ludo import LudoMatch, LudoParticipant

        ludo_stats_q = (
            db.query(
                LudoParticipant.user_id,
                func.count(LudoParticipant.id).label("matches"),
                func.sum(
                    case((LudoMatch.status == "COMPLETED", 1), else_=0)
                ).label("wins"),
            )
            .join(LudoMatch, LudoMatch.id == LudoParticipant.match_id)
            .filter(LudoMatch.status == "COMPLETED")
        )
        if range_start:
            ludo_stats_q = ludo_stats_q.filter(LudoMatch.updated_at >= range_start)
        ludo_stats_q = ludo_stats_q.group_by(LudoParticipant.user_id).subquery()

        ludo_earnings_q = (
            db.query(
                WalletTransaction.user_id,
                func.sum(WalletTransaction.amount).label("earnings"),
            )
            .filter(
                WalletTransaction.status == "SUCCESS",
                or_(
                    WalletTransaction.transaction_type == "LUDO_WIN",
                    WalletTransaction.transaction_type == "LUDO_PRIZE",
                    and_(
                        WalletTransaction.transaction_type == "PRIZE_WIN",
                        WalletTransaction.failure_reason.ilike("%ludo%"),
                    ),
                ),
            )
        )
        if range_start:
            ludo_earnings_q = ludo_earnings_q.filter(WalletTransaction.created_at >= range_start)
        ludo_earnings_subq = ludo_earnings_q.group_by(WalletTransaction.user_id).subquery()

        rows = (
            db.query(
                User,
                func.coalesce(ludo_stats_q.c.matches, 0).label("total_matches"),
                func.coalesce(ludo_stats_q.c.wins, 0).label("total_wins"),
                func.coalesce(ludo_earnings_subq.c.earnings, 0.0).label("total_earnings"),
            )
            .outerjoin(ludo_stats_q, User.id == ludo_stats_q.c.user_id)
            .outerjoin(ludo_earnings_subq, User.id == ludo_earnings_subq.c.user_id)
            .filter(User.is_active.is_(True), User.id < 99000)
            .filter(or_(ludo_stats_q.c.user_id.isnot(None), ludo_earnings_subq.c.user_id.isnot(None)))
            .order_by(
                func.coalesce(ludo_earnings_subq.c.earnings, 0.0).desc(),
                func.coalesce(ludo_stats_q.c.wins, 0).desc(),
                func.coalesce(ludo_stats_q.c.matches, 0).desc(),
                User.username.asc(),
            )
            .limit(safe_limit)
            .all()
        )

        return [
            {
                "rank": idx + 1,
                "id": row.User.id,
                "username": _mask_username(row.User.username),
                "profile_pic": row.User.profile_pic,
                "total_matches": int(row.total_matches or 0),
                "total_wins": int(row.total_wins or 0),
                "total_earnings": float(row.total_earnings or 0.0),
            }
            for idx, row in enumerate(rows)
        ]

    # ── Quiz ──────────────────────────────────────────────────────────────────
    if normalized_category == "quiz":
        from models.quiz import QuizParticipant, QuizMatch

        quiz_stats_q = (
            db.query(
                QuizParticipant.user_id,
                func.count(QuizParticipant.id).label("matches"),
                func.sum(
                    case((QuizParticipant.rank == 1, 1), else_=0)
                ).label("wins"),
            )
            .join(QuizMatch, QuizMatch.id == QuizParticipant.quiz_id)
            .filter(QuizParticipant.status == "COMPLETED")
        )
        if range_start:
            quiz_stats_q = quiz_stats_q.filter(QuizMatch.end_time >= range_start)
        quiz_stats_q = quiz_stats_q.group_by(QuizParticipant.user_id).subquery()

        quiz_earnings_q = (
            db.query(
                WalletTransaction.user_id,
                func.sum(WalletTransaction.amount).label("earnings"),
            )
            .filter(
                WalletTransaction.status == "SUCCESS",
                or_(
                    WalletTransaction.transaction_type == "QUIZ_WIN",
                    WalletTransaction.transaction_type == "QUIZ_PRIZE",
                    and_(
                        WalletTransaction.transaction_type == "PRIZE_WIN",
                        WalletTransaction.failure_reason.ilike("%quiz%"),
                    ),
                ),
            )
        )
        if range_start:
            quiz_earnings_q = quiz_earnings_q.filter(WalletTransaction.created_at >= range_start)
        quiz_earnings_subq = quiz_earnings_q.group_by(WalletTransaction.user_id).subquery()

        rows = (
            db.query(
                User,
                func.coalesce(quiz_stats_q.c.matches, 0).label("total_matches"),
                func.coalesce(quiz_stats_q.c.wins, 0).label("total_wins"),
                func.coalesce(quiz_earnings_subq.c.earnings, 0.0).label("total_earnings"),
            )
            .outerjoin(quiz_stats_q, User.id == quiz_stats_q.c.user_id)
            .outerjoin(quiz_earnings_subq, User.id == quiz_earnings_subq.c.user_id)
            .filter(User.is_active.is_(True), User.id < 99000)
            .filter(or_(quiz_stats_q.c.user_id.isnot(None), quiz_earnings_subq.c.user_id.isnot(None)))
            .order_by(
                func.coalesce(quiz_earnings_subq.c.earnings, 0.0).desc(),
                func.coalesce(quiz_stats_q.c.wins, 0).desc(),
                func.coalesce(quiz_stats_q.c.matches, 0).desc(),
                User.username.asc(),
            )
            .limit(safe_limit)
            .all()
        )

        return [
            {
                "rank": idx + 1,
                "id": row.User.id,
                "username": _mask_username(row.User.username),
                "profile_pic": row.User.profile_pic,
                "total_matches": int(row.total_matches or 0),
                "total_wins": int(row.total_wins or 0),
                "total_earnings": float(row.total_earnings or 0.0),
            }
            for idx, row in enumerate(rows)
        ]

    # ── Free Fire / FF Max / Clash Squad ──────────────────────────────────────
    game_patterns = {
        "free_fire_max": ["%free fire max%", "%free fire%max%", "%max%free fire%"],
        "clash_squad": ["%clash squad%", "%clash%"],
        "fan_battle": ["%fan battle%", "%fanbattle%", "%fan%battle%"],
        "free_fire": ["%free fire%", "%freefire%"],
    }

    selected_patterns = game_patterns.get(normalized_category, ["%"])
    game_filter = or_(*[Tournament.game_name.ilike(p) for p in selected_patterns])

    tournament_subq = (
        db.query(Tournament.id)
        .filter(Tournament.status == "COMPLETED", game_filter)
    )
    if range_start:
        tournament_subq = tournament_subq.filter(
            or_(
                Tournament.updated_at >= range_start,
                Tournament.match_time >= range_start,
            )
        )
    tournament_ids_subq = tournament_subq.subquery()

    stats_query = (
        db.query(
            TournamentParticipant.user_id,
            func.count(TournamentParticipant.id).label("matches"),
            func.sum(
                case(
                    (Tournament.winner_id == TournamentParticipant.user_id, 1),
                    else_=0,
                )
            ).label("wins"),
        )
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .filter(TournamentParticipant.tournament_id.in_(select(tournament_ids_subq.c.id)))
        .group_by(TournamentParticipant.user_id)
        .subquery()
    )

    prize_payment_mode = leaderboard_prize_payment_mode(normalized_category)
    earnings_query = (
        db.query(
            WalletTransaction.user_id,
            func.sum(WalletTransaction.amount).label("earnings"),
        )
        .filter(WalletTransaction.status == "SUCCESS")
    )
    if prize_payment_mode:
        earnings_query = earnings_query.filter(
            or_(
                WalletTransaction.transaction_type == prize_payment_mode,
                and_(
                    WalletTransaction.transaction_type == "PRIZE_WIN",
                    WalletTransaction.payment_mode == prize_payment_mode,
                ),
            )
        )
    else:
        earnings_query = earnings_query.filter(
            or_(
                WalletTransaction.transaction_type.ilike("%REWARD%"),
                WalletTransaction.transaction_type.ilike("%PRIZE%"),
                WalletTransaction.payment_mode.ilike("%PRIZE%"),
            )
        )

    if range_start:
        earnings_query = earnings_query.filter(WalletTransaction.created_at >= range_start)

    earnings_subq = earnings_query.group_by(WalletTransaction.user_id).subquery()

    leaderboard_users = (
        db.query(
            User,
            func.coalesce(stats_query.c.matches, 0).label("total_matches"),
            func.coalesce(stats_query.c.wins, 0).label("total_wins"),
            func.coalesce(earnings_subq.c.earnings, 0.0).label("total_earnings"),
        )
        .outerjoin(stats_query, User.id == stats_query.c.user_id)
        .outerjoin(earnings_subq, User.id == earnings_subq.c.user_id)
        .filter(User.is_active.is_(True))
        .filter(func.coalesce(earnings_subq.c.earnings, 0.0) > 0)
        .order_by(
            func.coalesce(earnings_subq.c.earnings, 0.0).desc(),
            func.coalesce(stats_query.c.wins, 0).desc(),
            func.coalesce(stats_query.c.matches, 0).desc(),
            User.username.asc(),
        )
        .limit(safe_limit)
        .all()
    )

    return [
        {
            "rank": idx + 1,
            "id": row.User.id,
            "username": _mask_username(row.User.username),
            "profile_pic": row.User.profile_pic,
            "total_matches": int(row.total_matches or 0),
            "total_wins": int(row.total_wins or 0),
            "total_earnings": float(row.total_earnings or 0.0),
        }
        for idx, row in enumerate(leaderboard_users)
    ]


@router.get("/withdrawals")

def get_public_withdrawals(
    db: Session = Depends(get_db),
    limit: int = Query(default=6, ge=1, le=20),
):
    safe_limit = max(1, min(limit, 20))

    rows = (
        db.query(
            WalletTransaction.id,
            WalletTransaction.amount,
            WalletTransaction.created_at,
            WalletTransaction.updated_at,
            User.username,
            User.profile_pic,
        )
        .join(User, User.id == WalletTransaction.user_id)
        .filter(
            WalletTransaction.transaction_type == "WITHDRAWAL",
            WalletTransaction.status == "SUCCESS",
            User.is_active.is_(True),
        )
        .order_by(func.coalesce(WalletTransaction.updated_at, WalletTransaction.created_at).desc())
        .limit(safe_limit)
        .all()
    )

    items = []
    for row in rows:
        amount_value = float(abs(row.amount or 0))
        items.append(
            {
                "id": row.id,
                "display_name": _mask_username(row.username),
                "profile_pic": row.profile_pic,
                "amount": amount_value,
                "created_at": row.updated_at or row.created_at,
            }
        )

    return {"items": items}
