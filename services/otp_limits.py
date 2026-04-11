from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.otp_phone_lock import OtpPhoneLock
from models.restriction import UserRestriction
from models.user import User
from services.restrictions import (
    RESTRICTION_SCOPE_FULL_APP,
    get_active_restrictions_for_user,
    get_active_restrictions_for_user_async,
    utcnow_naive,
)

OTP_MAX_SEND_ATTEMPTS = 5
OTP_LOCK_STATUS = "otp_locked"
OTP_LOCK_REASON = "OTP attempt limit exceeded: 5 OTP sends without successful verification."
OTP_LOCK_CLIENT_MESSAGE = "OTP limit reached for this number. Contact support on WhatsApp to continue."


def normalize_phone_for_otp_limit(raw_phone: str) -> str:
    phone = (raw_phone or "").strip().replace(" ", "")
    if len(phone) == 10 and phone.isdigit():
        return f"+91{phone}"

    if phone.startswith("+"):
        return phone

    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return phone


def _matches_otp_lock_reason(reason: str | None) -> bool:
    return (reason or "").strip().lower().startswith("otp attempt limit exceeded")


async def get_active_phone_lock_async(db: AsyncSession, phone: str) -> OtpPhoneLock | None:
    normalized_phone = normalize_phone_for_otp_limit(phone)
    if not normalized_phone:
        return None

    result = await db.execute(
        select(OtpPhoneLock).where(
            OtpPhoneLock.phone_number == normalized_phone,
            OtpPhoneLock.is_locked == True,
        )
    )
    return result.scalar_one_or_none()


def get_active_phone_lock_sync(db: Session, phone: str) -> OtpPhoneLock | None:
    normalized_phone = normalize_phone_for_otp_limit(phone)
    if not normalized_phone:
        return None

    return db.query(OtpPhoneLock).filter(
        OtpPhoneLock.phone_number == normalized_phone,
        OtpPhoneLock.is_locked == True,
    ).first()


async def _create_or_get_phone_lock_async(db: AsyncSession, phone: str) -> OtpPhoneLock:
    normalized_phone = normalize_phone_for_otp_limit(phone)
    result = await db.execute(select(OtpPhoneLock).where(OtpPhoneLock.phone_number == normalized_phone))
    lock = result.scalar_one_or_none()
    if lock:
        return lock

    lock = OtpPhoneLock(
        phone_number=normalized_phone,
        otp_send_count=0,
        is_locked=False,
    )
    db.add(lock)
    await db.flush()
    return lock


def _create_or_get_phone_lock_sync(db: Session, phone: str) -> OtpPhoneLock:
    normalized_phone = normalize_phone_for_otp_limit(phone)
    lock = db.query(OtpPhoneLock).filter(OtpPhoneLock.phone_number == normalized_phone).first()
    if lock:
        return lock

    lock = OtpPhoneLock(
        phone_number=normalized_phone,
        otp_send_count=0,
        is_locked=False,
    )
    db.add(lock)
    db.flush()
    return lock


async def _ensure_full_app_otp_restriction_async(db: AsyncSession, user: User, now: datetime) -> None:
    existing = await get_active_restrictions_for_user_async(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    has_same_reason = any(_matches_otp_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
        page_key=None,
        reason=OTP_LOCK_REASON,
        starts_at=now,
        ends_at=None,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


def _ensure_full_app_otp_restriction_sync(db: Session, user: User, now: datetime) -> None:
    existing = get_active_restrictions_for_user(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    has_same_reason = any(_matches_otp_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
        page_key=None,
        reason=OTP_LOCK_REASON,
        starts_at=now,
        ends_at=None,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


async def _unlock_system_otp_restrictions_async(
    db: AsyncSession,
    user_id: int,
    note: str,
    lifted_by_admin_id: int | None,
) -> None:
    now = utcnow_naive()
    rows = await db.execute(
        select(UserRestriction).where(
            UserRestriction.user_id == user_id,
            UserRestriction.scope == RESTRICTION_SCOPE_FULL_APP,
            UserRestriction.is_active == True,
        )
    )
    for restriction in rows.scalars().all():
        if not _matches_otp_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


def _unlock_system_otp_restrictions_sync(
    db: Session,
    user_id: int,
    note: str,
    lifted_by_admin_id: int | None,
) -> None:
    now = utcnow_naive()
    restrictions = db.query(UserRestriction).filter(
        UserRestriction.user_id == user_id,
        UserRestriction.scope == RESTRICTION_SCOPE_FULL_APP,
        UserRestriction.is_active == True,
    ).all()
    for restriction in restrictions:
        if not _matches_otp_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


async def register_otp_send_success_async(
    db: AsyncSession,
    phone: str,
    source: str,
    user: User | None = None,
) -> OtpPhoneLock:
    now = utcnow_naive()
    lock = await _create_or_get_phone_lock_async(db, phone)

    if user and user.id:
        lock.user_id = user.id

    lock.last_source = (source or "").strip().upper()[:40] or None
    lock.last_sent_at = now
    if lock.first_sent_at is None:
        lock.first_sent_at = now

    lock.otp_send_count = int(lock.otp_send_count or 0) + 1
    if lock.otp_send_count >= OTP_MAX_SEND_ATTEMPTS:
        lock.is_locked = True
        lock.locked_at = lock.locked_at or now
        lock.lock_reason = OTP_LOCK_REASON
        if user:
            user.is_active = False
            user.token_version = (getattr(user, "token_version", 0) or 0) + 1
            await _ensure_full_app_otp_restriction_async(db, user, now)
            db.add(user)

    db.add(lock)
    await db.commit()
    await db.refresh(lock)
    return lock


async def reset_otp_lock_after_success_async(db: AsyncSession, phone: str, user: User | None = None) -> None:
    normalized_phone = normalize_phone_for_otp_limit(phone)
    if not normalized_phone:
        return

    result = await db.execute(select(OtpPhoneLock).where(OtpPhoneLock.phone_number == normalized_phone))
    lock = result.scalar_one_or_none()
    if not lock:
        return

    now = utcnow_naive()
    lock.otp_send_count = 0
    lock.is_locked = False
    lock.locked_at = None
    lock.lock_reason = None
    lock.unlocked_at = now
    lock.last_source = "VERIFY_SUCCESS"
    lock.reset_note = "Auto-reset after successful OTP verification"

    if user and user.id:
        lock.user_id = user.id
        await _unlock_system_otp_restrictions_async(
            db,
            user_id=user.id,
            note="Auto-unlocked after successful OTP verification",
            lifted_by_admin_id=None,
        )
        remaining = await get_active_restrictions_for_user_async(
            db,
            user.id,
            scope=RESTRICTION_SCOPE_FULL_APP,
        )
        if not remaining:
            user.is_active = True
            db.add(user)

    db.add(lock)
    await db.commit()


def clear_otp_lock_for_user_sync(
    db: Session,
    user: User,
    admin_id: int | None,
    note: str,
) -> None:
    if not user.phone_number:
        return

    now = utcnow_naive()
    normalized_phone = normalize_phone_for_otp_limit(user.phone_number)
    lock = db.query(OtpPhoneLock).filter(OtpPhoneLock.phone_number == normalized_phone).first()
    if lock:
        lock.user_id = user.id
        lock.otp_send_count = 0
        lock.is_locked = False
        lock.locked_at = None
        lock.lock_reason = None
        lock.unlocked_at = now
        lock.unlocked_by_admin_id = admin_id
        lock.reset_note = note
        lock.last_source = "ADMIN_RESET"
        db.add(lock)

    _unlock_system_otp_restrictions_sync(
        db,
        user_id=user.id,
        note=note,
        lifted_by_admin_id=admin_id,
    )

    remaining = get_active_restrictions_for_user(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    if not remaining:
        user.is_active = True
        db.add(user)


def list_otp_locks_sync(db: Session, include_unlocked: bool = False) -> list[OtpPhoneLock]:
    query = db.query(OtpPhoneLock)
    if not include_unlocked:
        query = query.filter(OtpPhoneLock.is_locked == True)

    return query.order_by(
        OtpPhoneLock.locked_at.desc(),
        OtpPhoneLock.last_sent_at.desc(),
        OtpPhoneLock.id.desc(),
    ).all()


def reset_otp_lock_sync(
    db: Session,
    lock: OtpPhoneLock,
    admin_id: int,
    note: str,
) -> OtpPhoneLock:
    now = utcnow_naive()
    lock.otp_send_count = 0
    lock.is_locked = False
    lock.locked_at = None
    lock.lock_reason = None
    lock.unlocked_at = now
    lock.unlocked_by_admin_id = admin_id
    lock.reset_note = (note or "").strip() or "Unlocked from admin panel"
    lock.last_source = "ADMIN_RESET"

    user = db.query(User).filter(User.id == lock.user_id).first() if lock.user_id else None
    if user:
        _unlock_system_otp_restrictions_sync(
            db,
            user_id=user.id,
            note=lock.reset_note,
            lifted_by_admin_id=admin_id,
        )
        remaining = get_active_restrictions_for_user(
            db,
            user.id,
            scope=RESTRICTION_SCOPE_FULL_APP,
        )
        if not remaining:
            user.is_active = True
            db.add(user)

    db.add(lock)
    db.commit()
    db.refresh(lock)
    return lock
