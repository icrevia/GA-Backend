from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.orm import Session

from models.participant import TournamentParticipant
from models.tournament import Tournament
from models.ludo import LudoMatch, LudoParticipant

MODE_KEYS = ("free_fire", "fan_battle", "free_fire_max", "clash_squad", "ludo")
LEADERBOARD_CATEGORIES = ("free_fire", "free_fire_max", "clash_squad")
LEADERBOARD_PRIZE_PAYMENT_PREFIX = "LEADERBOARD_PRIZE:"


def _empty_mode_bucket() -> dict[str, int]:
    return {"matches": 0, "wins": 0, "win_rate": 0}


def empty_user_match_stats() -> dict[str, Any]:
    return {
        "free_fire": _empty_mode_bucket(),
        "fan_battle": _empty_mode_bucket(),
        "free_fire_max": _empty_mode_bucket(),
        "clash_squad": _empty_mode_bucket(),
        "ludo": _empty_mode_bucket(),
        "total_matches": 0,
        "total_wins": 0,
        "overall_win_rate": 0,
    }


def _safe_rate(matches: int, wins: int) -> int:
    if matches <= 0:
        return 0
    bounded_wins = max(0, min(wins, matches))
    return int(round((bounded_wins * 100.0) / matches))


def classify_game_mode(game_name: str | None) -> str | None:
    if not game_name:
        return None

    raw = " ".join(game_name.strip().lower().split())

    if "free fire max" in raw or ("free fire" in raw and "max" in raw):
        return "free_fire_max"
    if "clash squad" in raw or "clash" in raw:
        return "clash_squad"
    if "fan battle" in raw or "fanbattle" in raw or ("fan" in raw and "battle" in raw):
        return "fan_battle"
    if "free fire" in raw or "freefire" in raw:
        return "free_fire"
    return None


def normalize_leaderboard_category(raw: str | None) -> str | None:
    if not raw:
        return None

    clean = "_".join(raw.strip().lower().replace("-", "_").split())
    aliases = {
        "ff": "free_fire",
        "freefire": "free_fire",
        "free_fire": "free_fire",
        "ffmax": "free_fire_max",
        "freefiremax": "free_fire_max",
        "free_fire_max": "free_fire_max",
        "cs": "clash_squad",
        "clashsquad": "clash_squad",
        "clash_squad": "clash_squad",
    }
    normalized = aliases.get(clean, clean)
    if normalized in LEADERBOARD_CATEGORIES:
        return normalized
    return None


def leaderboard_prize_payment_mode(category: str | None) -> str | None:
    normalized = normalize_leaderboard_category(category)
    if not normalized:
        return None
    return f"{LEADERBOARD_PRIZE_PAYMENT_PREFIX}{normalized}"


def _finalize_user_stats(stats: dict[str, Any]) -> dict[str, Any]:
    total_matches = 0
    total_wins = 0

    for mode_key in MODE_KEYS:
        bucket = stats[mode_key]
        matches = int(bucket.get("matches", 0) or 0)
        wins = int(bucket.get("wins", 0) or 0)
        bucket["win_rate"] = _safe_rate(matches, wins)
        total_matches += matches
        total_wins += wins

    stats["total_matches"] = total_matches
    stats["total_wins"] = total_wins
    stats["overall_win_rate"] = _safe_rate(total_matches, total_wins)
    return stats


def compute_match_stats_for_user_ids(db: Session, user_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    unique_ids = sorted({int(uid) for uid in user_ids if int(uid) > 0})
    if not unique_ids:
        return {}

    stats_map: dict[int, dict[str, Any]] = {
        user_id: empty_user_match_stats()
        for user_id in unique_ids
    }

    rows = (
        db.query(
            TournamentParticipant.user_id,
            Tournament.game_name,
            Tournament.winner_id,
        )
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .filter(
            TournamentParticipant.user_id.in_(unique_ids),
            Tournament.status == "COMPLETED",
        )
        .all()
    )

    for user_id, game_name, winner_id in rows:
        mode = classify_game_mode(game_name)
        if mode is None:
            continue

        user_stats = stats_map.get(user_id)
        if not user_stats:
            continue

        bucket = user_stats[mode]
        bucket["matches"] += 1
        if winner_id == user_id:
            bucket["wins"] += 1

    ludo_rows = (
        db.query(
            LudoParticipant.user_id,
            LudoMatch.winner_id,
        )
        .join(LudoMatch, LudoMatch.id == LudoParticipant.match_id)
        .filter(
            LudoParticipant.user_id.in_(unique_ids),
            LudoMatch.status == "COMPLETED",
        )
        .all()
    )

    for uid, winner_id in ludo_rows:
        user_stats = stats_map.get(uid)
        if not user_stats:
            continue
            
        bucket = user_stats["ludo"]
        bucket["matches"] += 1
        if winner_id == uid:
            bucket["wins"] += 1

    for user_id in unique_ids:
        stats_map[user_id] = _finalize_user_stats(stats_map[user_id])

    return stats_map


def compute_match_stats_for_user(db: Session, user_id: int) -> dict[str, Any]:
    stats_map = compute_match_stats_for_user_ids(db, [user_id])
    return stats_map.get(user_id, empty_user_match_stats())
