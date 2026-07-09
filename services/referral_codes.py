from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.user import User

_FALLBACK_PREFIX = "GAMER"
_SEPARATOR = "-"
_SUFFIX_SPACE = 1000
_PREFIX_MAX_LEN = 48

_rng = secrets.SystemRandom()


def _normalize_username_prefix(username: str | None) -> str:
    collapsed = " ".join((username or "").strip().split())
    cleaned = "".join(ch for ch in collapsed.upper() if ch.isalnum())
    if not cleaned:
        return _FALLBACK_PREFIX

    if cleaned[0].isdigit():
        cleaned = f"G{cleaned}"

    return cleaned[:_PREFIX_MAX_LEN]


def is_username_aligned_referral_code(username: str | None, referral_code: str | None) -> bool:
    code = (referral_code or "").strip().upper()
    if not code:
        return False

    prefix = f"{_normalize_username_prefix(username)}{_SEPARATOR}"
    if not code.startswith(prefix):
        return False

    suffix = code[len(prefix):]
    return len(suffix) == 3 and suffix.isdigit()


def _build_candidate(prefix: str, suffix_number: int) -> str:
    return f"{prefix}{_SEPARATOR}{suffix_number:03d}"


def _build_shuffled_suffixes() -> list[int]:
    suffixes = list(range(_SUFFIX_SPACE))
    _rng.shuffle(suffixes)
    return suffixes


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

    for suffix_number in _build_shuffled_suffixes():
        candidate = _build_candidate(prefix, suffix_number)
        if _is_available_sync(db, candidate, user_id):
            return candidate

    raise RuntimeError(
        "Unable to allocate unique referral code for this name. "
        "Please update profile name and retry."
    )


async def generate_unique_referral_code_async(
    db: AsyncSession,
    username: str | None,
    user_id: int | None = None,
) -> str:
    prefix = _normalize_username_prefix(username)

    for suffix_number in _build_shuffled_suffixes():
        candidate = _build_candidate(prefix, suffix_number)
        if await _is_available_async(db, candidate, user_id):
            return candidate

    raise RuntimeError(
        "Unable to allocate unique referral code for this name. "
        "Please update profile name and retry."
    )
