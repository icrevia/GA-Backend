from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.restriction import UserRestriction


RESTRICTION_SCOPE_FULL_APP = "FULL_APP"
RESTRICTION_SCOPE_PAGE = "PAGE"

VALID_RESTRICTION_SCOPES = {
    RESTRICTION_SCOPE_FULL_APP,
    RESTRICTION_SCOPE_PAGE,
}

VALID_RESTRICTION_PAGE_KEYS = {
    "HOME",
    "TOURNAMENTS",
    "WALLET",
    "SPIN",
    "REFERRAL",
    "PROFILE",
    "SUPPORT",
    "QUIZ",
}

_PAGE_KEY_ALIASES = {
    "TOURNAMENT": "TOURNAMENTS",
    "TOURNAMENTS": "TOURNAMENTS",
    "REFERRALS": "REFERRAL",
    "HELP": "SUPPORT",
    "LIVE_CHAT": "SUPPORT",
    "PAYMENTS": "WALLET",
    "PAYMENT": "WALLET",
    "QUIZZES": "QUIZ",
}


def utcnow_naive() -> datetime:
    return datetime.utcnow()


def to_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.replace(tzinfo=None)
    return dt


def normalize_restriction_scope(raw_value: Optional[str]) -> str:
    value = (raw_value or "").strip().upper()
    if value not in VALID_RESTRICTION_SCOPES:
        raise ValueError("Invalid restriction scope. Use FULL_APP or PAGE")
    return value


def normalize_restriction_page_key(raw_value: Optional[str]) -> Optional[str]:
    if raw_value is None:
        return None

    cleaned = "_".join(raw_value.strip().upper().replace("-", "_").split())
    if not cleaned:
        return None

    normalized = _PAGE_KEY_ALIASES.get(cleaned, cleaned)
    if normalized not in VALID_RESTRICTION_PAGE_KEYS:
        raise ValueError(
            "Invalid page_key. Supported values: "
            + ", ".join(sorted(VALID_RESTRICTION_PAGE_KEYS))
        )
    return normalized


def is_restriction_currently_active(
    restriction: UserRestriction,
    now: Optional[datetime] = None,
) -> bool:
    if not bool(restriction.is_active):
        return False

    now_value = now or utcnow_naive()
    starts_at = to_naive(restriction.starts_at) or to_naive(restriction.created_at) or now_value
    ends_at = to_naive(restriction.ends_at)

    if starts_at > now_value:
        return False
    if ends_at is not None and ends_at <= now_value:
        return False

    return True


def get_active_restrictions_for_user(
    db: Session,
    user_id: int,
    scope: Optional[str] = None,
    page_key: Optional[str] = None,
) -> list[UserRestriction]:
    query = db.query(UserRestriction).filter(
        UserRestriction.user_id == user_id,
        UserRestriction.is_active == True,
    )

    if scope is not None:
        query = query.filter(UserRestriction.scope == normalize_restriction_scope(scope))

    restrictions = query.order_by(UserRestriction.created_at.desc()).all()
    normalized_page_key = normalize_restriction_page_key(page_key) if page_key is not None else None

    now_value = utcnow_naive()
    result: list[UserRestriction] = []
    for restriction in restrictions:
        if not is_restriction_currently_active(restriction, now_value):
            continue

        if normalized_page_key is not None:
            if restriction.scope != RESTRICTION_SCOPE_PAGE:
                continue
            if normalize_restriction_page_key(restriction.page_key) != normalized_page_key:
                continue

        result.append(restriction)

    return result


async def get_active_restrictions_for_user_async(
    db: AsyncSession,
    user_id: int,
    scope: Optional[str] = None,
    page_key: Optional[str] = None,
) -> list[UserRestriction]:
    stmt = select(UserRestriction).where(
        UserRestriction.user_id == user_id,
        UserRestriction.is_active == True,
    )

    if scope is not None:
        stmt = stmt.where(UserRestriction.scope == normalize_restriction_scope(scope))

    stmt = stmt.order_by(UserRestriction.created_at.desc())
    rows = await db.execute(stmt)
    restrictions = list(rows.scalars().all())

    normalized_page_key = normalize_restriction_page_key(page_key) if page_key is not None else None
    now_value = utcnow_naive()

    result: list[UserRestriction] = []
    for restriction in restrictions:
        if not is_restriction_currently_active(restriction, now_value):
            continue

        if normalized_page_key is not None:
            if restriction.scope != RESTRICTION_SCOPE_PAGE:
                continue
            if normalize_restriction_page_key(restriction.page_key) != normalized_page_key:
                continue

        result.append(restriction)

    return result


def build_restriction_detail(restriction: UserRestriction) -> str:
    scope = restriction.scope or RESTRICTION_SCOPE_FULL_APP
    reason = (restriction.reason or "Access is restricted by admin").strip()
    ends_at = to_naive(restriction.ends_at)

    if scope == RESTRICTION_SCOPE_PAGE:
        page_key = normalize_restriction_page_key(restriction.page_key) or "THIS_SECTION"
        if ends_at:
            return f"Access to {page_key} is restricted until {ends_at.isoformat()} ({reason})"
        return f"Access to {page_key} is restricted ({reason})"

    if ends_at:
        return f"Account is restricted until {ends_at.isoformat()} ({reason})"
    return f"Account is restricted ({reason})"


def serialize_user_restriction(restriction: UserRestriction) -> dict:
    return {
        "id": restriction.id,
        "scope": restriction.scope,
        "page_key": normalize_restriction_page_key(restriction.page_key),
        "reason": restriction.reason,
        "starts_at": restriction.starts_at,
        "ends_at": restriction.ends_at,
        "created_at": restriction.created_at,
    }
