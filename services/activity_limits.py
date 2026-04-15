from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models.restriction import UserRestriction
from models.user import User
from models.user_activity_lock import UserActivityLock
from services.restrictions import (
    RESTRICTION_SCOPE_FULL_APP,
    RESTRICTION_SCOPE_PAGE,
    get_active_restrictions_for_user,
    get_active_restrictions_for_user_async,
    to_naive,
    utcnow_naive,
)

IST = timezone(timedelta(hours=5, minutes=30))
DAILY_RESET_MINUTE_IST = 1

ACTIVITY_PAYMENT_INIT = "PAYMENT_INIT"
ACTIVITY_LOGIN_SESSION = "LOGIN_SESSION"

PAYMENT_INIT_MAX_DAILY_ATTEMPTS = 20
PAYMENT_INIT_FAILURE_STREAK_LIMIT = 5
PAYMENT_INIT_LOCK_STATUS = "payment_init_locked"
PAYMENT_INIT_LOCK_REASON = "Payment initiation blocked: 5 failed add-money attempts without a successful payment."
PAYMENT_INIT_LOCK_CLIENT_MESSAGE = "Too many failed payment attempts. Resets at 12:01 AM IST or contact support on WhatsApp."
PAYMENT_INIT_DAILY_LIMIT_CLIENT_MESSAGE = "Daily payment initiation limit reached (20/day). Resets at 12:01 AM IST."

LOGIN_SESSION_MAX_DAILY_EVENTS = 5
LOGIN_SESSION_LOCK_STATUS = "login_session_locked"
LOGIN_SESSION_LOCK_REASON = "Login session limit exceeded: more than 5 successful logins in one day."
LOGIN_SESSION_LOCK_CLIENT_MESSAGE = "Login limit reached for today. Resets at 12:01 AM IST or contact support."


def _current_daily_cycle_ist(reset_minute_ist: int = DAILY_RESET_MINUTE_IST) -> tuple[str, datetime, datetime]:
    now_ist = datetime.now(IST)
    reset_point_ist = now_ist.replace(
        hour=0,
        minute=reset_minute_ist,
        second=0,
        microsecond=0,
    )
    cycle_start_ist = reset_point_ist - timedelta(days=1) if now_ist < reset_point_ist else reset_point_ist
    cycle_end_ist = cycle_start_ist + timedelta(days=1)
    cycle_key = cycle_start_ist.date().isoformat()
    cycle_start_utc = cycle_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    cycle_end_utc = cycle_end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return cycle_key, cycle_start_utc, cycle_end_utc


def _next_daily_reset_utc_naive(reset_minute_ist: int = DAILY_RESET_MINUTE_IST) -> datetime:
    _, _, cycle_end_utc = _current_daily_cycle_ist(reset_minute_ist)
    return cycle_end_utc


def _matches_payment_lock_reason(reason: str | None) -> bool:
    return (reason or "").strip().lower().startswith("payment initiation blocked")


def _matches_login_lock_reason(reason: str | None) -> bool:
    return (reason or "").strip().lower().startswith("login session limit exceeded")


def _reset_daily_counter_if_cycle_changed(lock: UserActivityLock) -> bool:
    cycle_key, _, _ = _current_daily_cycle_ist()
    if (lock.cycle_key or "").strip() == cycle_key:
        return False

    lock.cycle_key = cycle_key
    lock.daily_count = 0
    return True


async def _create_or_get_activity_lock_async(
    db: AsyncSession,
    user_id: int,
    activity_type: str,
) -> UserActivityLock:
    result = await db.execute(
        select(UserActivityLock).where(
            UserActivityLock.user_id == user_id,
            UserActivityLock.activity_type == activity_type,
        )
    )
    lock = result.scalar_one_or_none()
    if lock:
        return lock

    lock = UserActivityLock(
        user_id=user_id,
        activity_type=activity_type,
        daily_count=0,
        failed_streak=0,
        is_locked=False,
    )
    db.add(lock)
    await db.flush()
    return lock


def _create_or_get_activity_lock_sync(
    db: Session,
    user_id: int,
    activity_type: str,
) -> UserActivityLock:
    lock = (
        db.query(UserActivityLock)
        .filter(
            UserActivityLock.user_id == user_id,
            UserActivityLock.activity_type == activity_type,
        )
        .first()
    )
    if lock:
        return lock

    lock = UserActivityLock(
        user_id=user_id,
        activity_type=activity_type,
        daily_count=0,
        failed_streak=0,
        is_locked=False,
    )
    db.add(lock)
    db.flush()
    return lock


async def _ensure_wallet_payment_restriction_async(
    db: AsyncSession,
    user_id: int,
    now: datetime,
) -> None:
    existing = await get_active_restrictions_for_user_async(
        db,
        user_id,
        scope=RESTRICTION_SCOPE_PAGE,
        page_key="WALLET",
    )
    has_same_reason = any(_matches_payment_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user_id,
        scope=RESTRICTION_SCOPE_PAGE,
        page_key="WALLET",
        reason=PAYMENT_INIT_LOCK_REASON,
        starts_at=now,
        ends_at=None,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


def _ensure_wallet_payment_restriction_sync(
    db: Session,
    user_id: int,
    now: datetime,
) -> None:
    existing = get_active_restrictions_for_user(
        db,
        user_id,
        scope=RESTRICTION_SCOPE_PAGE,
        page_key="WALLET",
    )
    has_same_reason = any(_matches_payment_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user_id,
        scope=RESTRICTION_SCOPE_PAGE,
        page_key="WALLET",
        reason=PAYMENT_INIT_LOCK_REASON,
        starts_at=now,
        ends_at=None,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


async def _ensure_full_app_login_restriction_async(
    db: AsyncSession,
    user_id: int,
    now: datetime,
    ends_at: datetime,
) -> None:
    existing = await get_active_restrictions_for_user_async(
        db,
        user_id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    has_same_reason = any(_matches_login_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user_id,
        scope=RESTRICTION_SCOPE_FULL_APP,
        page_key=None,
        reason=LOGIN_SESSION_LOCK_REASON,
        starts_at=now,
        ends_at=ends_at,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


def _ensure_full_app_login_restriction_sync(
    db: Session,
    user_id: int,
    now: datetime,
    ends_at: datetime,
) -> None:
    existing = get_active_restrictions_for_user(
        db,
        user_id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    has_same_reason = any(_matches_login_lock_reason(item.reason) for item in existing)
    if has_same_reason:
        return

    restriction = UserRestriction(
        user_id=user_id,
        scope=RESTRICTION_SCOPE_FULL_APP,
        page_key=None,
        reason=LOGIN_SESSION_LOCK_REASON,
        starts_at=now,
        ends_at=ends_at,
        is_active=True,
        created_by_admin_id=None,
    )
    db.add(restriction)


async def _unlock_payment_restrictions_async(
    db: AsyncSession,
    user_id: int,
    note: str,
    lifted_by_admin_id: int | None,
) -> None:
    now = utcnow_naive()
    rows = await db.execute(
        select(UserRestriction).where(
            UserRestriction.user_id == user_id,
            UserRestriction.scope == RESTRICTION_SCOPE_PAGE,
            UserRestriction.is_active == True,
        )
    )
    for restriction in rows.scalars().all():
        if not _matches_payment_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


def _unlock_payment_restrictions_sync(
    db: Session,
    user_id: int,
    note: str,
    lifted_by_admin_id: int | None,
) -> None:
    now = utcnow_naive()
    restrictions = db.query(UserRestriction).filter(
        UserRestriction.user_id == user_id,
        UserRestriction.scope == RESTRICTION_SCOPE_PAGE,
        UserRestriction.is_active == True,
    ).all()
    for restriction in restrictions:
        if not _matches_payment_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


async def _unlock_login_restrictions_async(
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
        if not _matches_login_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


def _unlock_login_restrictions_sync(
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
        if not _matches_login_lock_reason(restriction.reason):
            continue
        restriction.is_active = False
        restriction.lifted_at = now
        restriction.lift_note = note
        restriction.lifted_by_admin_id = lifted_by_admin_id
        db.add(restriction)


async def _unlock_login_lock_async(
    db: AsyncSession,
    user: User,
    lock: UserActivityLock,
    *,
    note: str,
    admin_id: int | None,
    now: datetime,
) -> None:
    lock.is_locked = False
    lock.lock_status = None
    lock.lock_reason = None
    lock.locked_at = None
    lock.lock_expires_at = None
    lock.unlocked_at = now
    lock.unlocked_by_admin_id = admin_id
    lock.reset_note = note
    lock.failed_streak = 0
    lock.daily_count = 0
    lock.last_success_at = now
    db.add(lock)

    await _unlock_login_restrictions_async(
        db,
        user_id=user.id,
        note=note,
        lifted_by_admin_id=admin_id,
    )

    remaining = await get_active_restrictions_for_user_async(
        db,
        user.id,
        scope=RESTRICTION_SCOPE_FULL_APP,
    )
    if not remaining:
        user.is_active = True
        db.add(user)


def register_payment_init_attempt_sync(db: Session, user_id: int) -> UserActivityLock:
    now = utcnow_naive()
    lock = _create_or_get_activity_lock_sync(db, user_id=user_id, activity_type=ACTIVITY_PAYMENT_INIT)
    cycle_changed = _reset_daily_counter_if_cycle_changed(lock)

    if cycle_changed:
        lock.failed_streak = 0
        if bool(lock.is_locked) and (lock.lock_status or "") == PAYMENT_INIT_LOCK_STATUS:
            lock.is_locked = False
            lock.lock_status = None
            lock.lock_reason = None
            lock.locked_at = None
            lock.lock_expires_at = None
            lock.unlocked_at = now
            lock.unlocked_by_admin_id = None
            lock.reset_note = "Auto-reset at 12:01 AM IST daily rollover"
            _unlock_payment_restrictions_sync(
                db,
                user_id=user_id,
                note="Auto-unlocked at 12:01 AM IST daily rollover",
                lifted_by_admin_id=None,
            )

    if bool(lock.is_locked) and (lock.lock_status or "") == PAYMENT_INIT_LOCK_STATUS:
        raise HTTPException(status_code=429, detail=PAYMENT_INIT_LOCK_CLIENT_MESSAGE)

    daily_count = int(lock.daily_count or 0)
    if daily_count >= PAYMENT_INIT_MAX_DAILY_ATTEMPTS:
        raise HTTPException(status_code=429, detail=PAYMENT_INIT_DAILY_LIMIT_CLIENT_MESSAGE)

    lock.daily_count = daily_count + 1
    lock.last_attempt_at = now
    db.add(lock)
    db.commit()
    db.refresh(lock)
    return lock


def register_payment_failure_sync(
    db: Session,
    user_id: int,
    failure_reason: str | None = None,
) -> UserActivityLock:
    now = utcnow_naive()
    lock = _create_or_get_activity_lock_sync(db, user_id=user_id, activity_type=ACTIVITY_PAYMENT_INIT)
    cycle_changed = _reset_daily_counter_if_cycle_changed(lock)
    if cycle_changed:
        lock.failed_streak = 0

    lock.failed_streak = int(lock.failed_streak or 0) + 1
    lock.last_attempt_at = now

    if int(lock.failed_streak or 0) >= PAYMENT_INIT_FAILURE_STREAK_LIMIT:
        lock.is_locked = True
        lock.lock_status = PAYMENT_INIT_LOCK_STATUS
        lock.lock_reason = PAYMENT_INIT_LOCK_REASON if not failure_reason else PAYMENT_INIT_LOCK_REASON
        lock.locked_at = lock.locked_at or now
        lock.lock_expires_at = None
        _ensure_wallet_payment_restriction_sync(db, user_id=user_id, now=now)

    db.add(lock)
    return lock


def register_payment_success_sync(db: Session, user_id: int) -> None:
    lock = (
        db.query(UserActivityLock)
        .filter(
            UserActivityLock.user_id == user_id,
            UserActivityLock.activity_type == ACTIVITY_PAYMENT_INIT,
        )
        .first()
    )
    if not lock:
        return

    now = utcnow_naive()
    lock.failed_streak = 0
    lock.last_success_at = now

    if bool(lock.is_locked) and (lock.lock_status or "") == PAYMENT_INIT_LOCK_STATUS:
        lock.is_locked = False
        lock.lock_status = None
        lock.lock_reason = None
        lock.locked_at = None
        lock.lock_expires_at = None
        lock.unlocked_at = now
        lock.unlocked_by_admin_id = None
        lock.reset_note = "Auto-reset after successful payment"
        _unlock_payment_restrictions_sync(
            db,
            user_id=user_id,
            note="Auto-unlocked after successful payment",
            lifted_by_admin_id=None,
        )

    db.add(lock)


async def ensure_login_session_lock_not_blocking_async(db: AsyncSession, user: User) -> None:
    if user.role == "ADMIN":
        return

    now = utcnow_naive()
    lock = await _create_or_get_activity_lock_async(
        db,
        user_id=user.id,
        activity_type=ACTIVITY_LOGIN_SESSION,
    )
    changed = _reset_daily_counter_if_cycle_changed(lock)

    if changed and bool(lock.is_locked) and (lock.lock_status or "") == LOGIN_SESSION_LOCK_STATUS:
        await _unlock_login_lock_async(
            db,
            user=user,
            lock=lock,
            note="Auto-unlocked at 12:01 AM IST daily rollover",
            admin_id=None,
            now=now,
        )
        await db.commit()
        return

    if bool(lock.is_locked) and (lock.lock_status or "") == LOGIN_SESSION_LOCK_STATUS:
        lock_expires_at = to_naive(lock.lock_expires_at)
        if lock_expires_at is not None and lock_expires_at <= now:
            await _unlock_login_lock_async(
                db,
                user=user,
                lock=lock,
                note="Auto-unlocked at 12:01 AM IST daily rollover",
                admin_id=None,
                now=now,
            )
            await db.commit()
            return

        raise HTTPException(status_code=429, detail=LOGIN_SESSION_LOCK_CLIENT_MESSAGE)

    daily_count = int(lock.daily_count or 0)
    if daily_count >= LOGIN_SESSION_MAX_DAILY_EVENTS:
        lock.is_locked = True
        lock.lock_status = LOGIN_SESSION_LOCK_STATUS
        lock.lock_reason = LOGIN_SESSION_LOCK_REASON
        lock.locked_at = lock.locked_at or now
        lock.lock_expires_at = _next_daily_reset_utc_naive()
        lock.unlocked_at = None
        lock.unlocked_by_admin_id = None
        lock.reset_note = "Auto-lock before OTP send due to daily login session limit (resets 12:01 AM IST)"

        user.is_active = False
        user.token_version = (getattr(user, "token_version", 0) or 0) + 1
        db.add(user)

        await _ensure_full_app_login_restriction_async(
            db,
            user_id=user.id,
            now=now,
            ends_at=lock.lock_expires_at,
        )
        db.add(lock)
        await db.commit()
        raise HTTPException(status_code=429, detail=LOGIN_SESSION_LOCK_CLIENT_MESSAGE)

    if changed:
        db.add(lock)
        await db.commit()


async def register_login_session_success_async(db: AsyncSession, user: User) -> None:
    if user.role == "ADMIN":
        return

    now = utcnow_naive()
    lock = await _create_or_get_activity_lock_async(
        db,
        user_id=user.id,
        activity_type=ACTIVITY_LOGIN_SESSION,
    )
    changed = _reset_daily_counter_if_cycle_changed(lock)

    if changed and bool(lock.is_locked) and (lock.lock_status or "") == LOGIN_SESSION_LOCK_STATUS:
        await _unlock_login_lock_async(
            db,
            user=user,
            lock=lock,
            note="Auto-unlocked at 12:01 AM IST daily rollover",
            admin_id=None,
            now=now,
        )

    if bool(lock.is_locked) and (lock.lock_status or "") == LOGIN_SESSION_LOCK_STATUS:
        lock_expires_at = to_naive(lock.lock_expires_at)
        if lock_expires_at is not None and lock_expires_at <= now:
            await _unlock_login_lock_async(
                db,
                user=user,
                lock=lock,
                note="Auto-unlocked at 12:01 AM IST daily rollover",
                admin_id=None,
                now=now,
            )
        else:
            raise HTTPException(status_code=429, detail=LOGIN_SESSION_LOCK_CLIENT_MESSAGE)

    daily_count = int(lock.daily_count or 0)
    if daily_count >= LOGIN_SESSION_MAX_DAILY_EVENTS:
        lock.is_locked = True
        lock.lock_status = LOGIN_SESSION_LOCK_STATUS
        lock.lock_reason = LOGIN_SESSION_LOCK_REASON
        lock.locked_at = lock.locked_at or now
        lock.lock_expires_at = _next_daily_reset_utc_naive()
        lock.unlocked_at = None
        lock.unlocked_by_admin_id = None
        lock.reset_note = "Auto-lock after excessive login sessions (resets 12:01 AM IST)"

        user.is_active = False
        user.token_version = (getattr(user, "token_version", 0) or 0) + 1
        db.add(user)

        await _ensure_full_app_login_restriction_async(
            db,
            user_id=user.id,
            now=now,
            ends_at=lock.lock_expires_at,
        )
        db.add(lock)
        await db.commit()
        raise HTTPException(status_code=429, detail=LOGIN_SESSION_LOCK_CLIENT_MESSAGE)

    lock.daily_count = daily_count + 1
    lock.last_attempt_at = now
    lock.last_success_at = now
    db.add(lock)


def list_activity_locks_sync(
    db: Session,
    include_unlocked: bool = False,
    activity_type: str | None = None,
) -> list[UserActivityLock]:
    query = db.query(UserActivityLock)
    if activity_type:
        query = query.filter(UserActivityLock.activity_type == activity_type.strip().upper())
    if not include_unlocked:
        query = query.filter(UserActivityLock.is_locked == True)

    return query.order_by(
        UserActivityLock.locked_at.desc(),
        UserActivityLock.last_attempt_at.desc(),
        UserActivityLock.id.desc(),
    ).all()


def clear_activity_locks_for_user_sync(
    db: Session,
    user: User,
    admin_id: int | None,
    note: str,
) -> None:
    now = utcnow_naive()
    locks = (
        db.query(UserActivityLock)
        .filter(UserActivityLock.user_id == user.id)
        .with_for_update()
        .all()
    )

    for lock in locks:
        lock.cycle_key = None
        lock.daily_count = 0
        lock.failed_streak = 0
        lock.is_locked = False
        lock.lock_status = None
        lock.lock_reason = None
        lock.locked_at = None
        lock.lock_expires_at = None
        lock.unlocked_at = now
        lock.unlocked_by_admin_id = admin_id
        lock.reset_note = note
        lock.last_success_at = now
        db.add(lock)

        if lock.activity_type == ACTIVITY_PAYMENT_INIT:
            _unlock_payment_restrictions_sync(
                db,
                user_id=user.id,
                note=note,
                lifted_by_admin_id=admin_id,
            )
        elif lock.activity_type == ACTIVITY_LOGIN_SESSION:
            _unlock_login_restrictions_sync(
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


def reset_activity_lock_sync(
    db: Session,
    lock: UserActivityLock,
    admin_id: int,
    note: str,
) -> UserActivityLock:
    user = db.query(User).filter(User.id == lock.user_id).first()
    now = utcnow_naive()

    lock.cycle_key = None
    lock.daily_count = 0
    lock.failed_streak = 0
    lock.is_locked = False
    lock.lock_status = None
    lock.lock_reason = None
    lock.locked_at = None
    lock.lock_expires_at = None
    lock.unlocked_at = now
    lock.unlocked_by_admin_id = admin_id
    lock.reset_note = (note or "").strip() or "Unlocked from admin panel"
    lock.last_success_at = now

    if lock.activity_type == ACTIVITY_PAYMENT_INIT:
        _unlock_payment_restrictions_sync(
            db,
            user_id=lock.user_id,
            note=lock.reset_note,
            lifted_by_admin_id=admin_id,
        )
    elif lock.activity_type == ACTIVITY_LOGIN_SESSION:
        _unlock_login_restrictions_sync(
            db,
            user_id=lock.user_id,
            note=lock.reset_note,
            lifted_by_admin_id=admin_id,
        )

    if user:
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
