from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.user import User

_FALLBACK_PREFIX = "GAMER"
_SEPARATOR = "-"
_WORD_COUNT = 3
_CANDIDATE_ATTEMPTS = 300
_WORD_BANK = (
    "ALPHA",
    "ARROW",
    "ASTRO",
    "BLAZE",
    "BOLT",
    "BRAVO",
    "CARGO",
    "CHAMP",
    "CLOUD",
    "COMET",
    "CRISP",
    "CROWN",
    "DART",
    "DELTA",
    "DUSK",
    "ECHO",
    "EMBER",
    "FALCON",
    "FLARE",
    "FLASH",
    "FROST",
    "GHOST",
    "GLIDE",
    "GLOW",
    "HAWK",
    "HYPER",
    "ION",
    "JET",
    "KNIGHT",
    "LASER",
    "LEGEND",
    "LUNAR",
    "MATRIX",
    "METRO",
    "NEXUS",
    "NITRO",
    "NOVA",
    "OMEGA",
    "ORBIT",
    "PHANTOM",
    "PHOENIX",
    "PULSE",
    "QUARK",
    "QUEST",
    "RAPTOR",
    "RIDER",
    "RIVAL",
    "ROCKET",
    "SCOUT",
    "SHADOW",
    "SKY",
    "SNAP",
    "SONIC",
    "SPARK",
    "SPIRIT",
    "STORM",
    "STRIKE",
    "SWIFT",
    "TANGO",
    "THOR",
    "TIGER",
    "TITAN",
    "TURBO",
    "VORTEX",
    "WAVE",
    "WOLF",
    "ZENITH",
)


def _normalize_username_prefix(username: str | None) -> str:
    cleaned = "".join(ch for ch in (username or "").upper() if ch.isalnum())
    if not cleaned:
        return _FALLBACK_PREFIX

    if cleaned[0].isdigit():
        cleaned = f"G{cleaned}"

    return cleaned


def is_username_aligned_referral_code(username: str | None, referral_code: str | None) -> bool:
    code = (referral_code or "").strip().upper()
    if not code:
        return False

    prefix = f"{_normalize_username_prefix(username)}{_SEPARATOR}"
    if not code.startswith(prefix):
        return False

    parts = code.split(_SEPARATOR)
    return len(parts) >= (_WORD_COUNT + 1) and all(part for part in parts)


def _build_candidate(prefix: str) -> str:
    suffix_parts = [secrets.choice(_WORD_BANK) for _ in range(_WORD_COUNT)]
    return f"{prefix}{_SEPARATOR}{_SEPARATOR.join(suffix_parts)}"


def _build_fallback_candidate(prefix: str) -> str:
    suffix_parts = [
        secrets.choice(_WORD_BANK),
        secrets.choice(_WORD_BANK),
        f"{secrets.choice(_WORD_BANK)}{secrets.randbelow(1000):03d}",
    ]
    return f"{prefix}{_SEPARATOR}{_SEPARATOR.join(suffix_parts)}"


def _is_available_sync(db: Session, candidate: str, user_id: int | None) -> bool:
    owner = db.query(User.id).filter(User.referral_code == candidate).first()
    if not owner:
        return True
    return user_id is not None and int(owner[0]) == int(user_id)


async def _is_available_async(db: AsyncSession, candidate: str, user_id: int | None) -> bool:
    owner_id = (await db.execute(select(User.id).where(User.referral_code == candidate))).scalar_one_or_none()
    if owner_id is None:
        return True
    return user_id is not None and int(owner_id) == int(user_id)


def generate_unique_referral_code_sync(
    db: Session,
    username: str | None,
    user_id: int | None = None,
) -> str:
    prefix = _normalize_username_prefix(username)

    for _ in range(_CANDIDATE_ATTEMPTS):
        candidate = _build_candidate(prefix)
        if _is_available_sync(db, candidate, user_id):
            return candidate

    while True:
        candidate = _build_fallback_candidate(prefix)
        if _is_available_sync(db, candidate, user_id):
            return candidate


async def generate_unique_referral_code_async(
    db: AsyncSession,
    username: str | None,
    user_id: int | None = None,
) -> str:
    prefix = _normalize_username_prefix(username)

    for _ in range(_CANDIDATE_ATTEMPTS):
        candidate = _build_candidate(prefix)
        if await _is_available_async(db, candidate, user_id):
            return candidate

    while True:
        candidate = _build_fallback_candidate(prefix)
        if await _is_available_async(db, candidate, user_id):
            return candidate
