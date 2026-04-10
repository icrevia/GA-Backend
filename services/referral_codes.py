from __future__ import annotations

import secrets
import string

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.user import User

_PREFIX_LEN = 5
_MIN_PREFIX_LEN = 4
_SUFFIX_ATTEMPTS = ((3, 10), (4, 30), (5, 60), (6, 120))
_ALPHABET = string.ascii_uppercase + string.digits
_FALLBACK_PREFIX = "GAMER"


def _normalize_username_prefix(username: str | None) -> str:
    cleaned = "".join(ch for ch in (username or "").upper() if ch.isalnum())
    if not cleaned:
        return _FALLBACK_PREFIX

    if cleaned[0].isdigit():
        cleaned = f"G{cleaned}"

    prefix = cleaned[:_PREFIX_LEN]
    if len(prefix) < _MIN_PREFIX_LEN:
        prefix = (prefix + _FALLBACK_PREFIX)[:_MIN_PREFIX_LEN]
    return prefix


def is_username_aligned_referral_code(username: str | None, referral_code: str | None) -> bool:
    code = (referral_code or "").strip().upper()
    if not code:
        return False
    return code.startswith(_normalize_username_prefix(username))


def _build_candidate(prefix: str, suffix_len: int) -> str:
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(suffix_len))
    return f"{prefix}{suffix}"


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

    for suffix_len, attempts in _SUFFIX_ATTEMPTS:
        for _ in range(attempts):
            candidate = _build_candidate(prefix, suffix_len)
            if _is_available_sync(db, candidate, user_id):
                return candidate

    while True:
        candidate = f"{prefix}{secrets.token_hex(4).upper()}"
        if _is_available_sync(db, candidate, user_id):
            return candidate


async def generate_unique_referral_code_async(
    db: AsyncSession,
    username: str | None,
    user_id: int | None = None,
) -> str:
    prefix = _normalize_username_prefix(username)

    for suffix_len, attempts in _SUFFIX_ATTEMPTS:
        for _ in range(attempts):
            candidate = _build_candidate(prefix, suffix_len)
            if await _is_available_async(db, candidate, user_id):
                return candidate

    while True:
        candidate = f"{prefix}{secrets.token_hex(4).upper()}"
        if await _is_available_async(db, candidate, user_id):
            return candidate
