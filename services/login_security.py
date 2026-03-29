from collections import deque
from datetime import datetime, timedelta, timezone
import ipaddress
from threading import Lock

from fastapi import Request

from core.config import settings


_FAILED_LOGIN_BUCKETS: dict[str, deque[datetime]] = {}
_BLOCKED_UNTIL: dict[str, datetime] = {}
_LOCK = Lock()


def extract_client_ip(request: Request) -> str:
    # Prefer trusted proxy/CDN headers in priority order, then fallback.
    candidates = [
        request.headers.get("cf-connecting-ip"),
        request.headers.get("true-client-ip"),
        request.headers.get("x-real-ip"),
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-client-ip"),
        request.headers.get("fastly-client-ip"),
    ]

    for candidate in candidates:
        if not candidate:
            continue

        raw_ip = candidate.split(",")[0].strip()
        if not raw_ip:
            continue

        try:
            ipaddress.ip_address(raw_ip)
            return raw_ip
        except ValueError:
            continue

    if request.client and request.client.host:
        host = request.client.host.strip()
        if host:
            return host

    return "unknown"


def _prune_old_attempts(ip: str, now: datetime) -> deque[datetime]:
    window_start = now - timedelta(seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS)
    bucket = _FAILED_LOGIN_BUCKETS.setdefault(ip, deque())
    while bucket and bucket[0] < window_start:
        bucket.popleft()
    return bucket


def is_ip_blocked(ip: str) -> tuple[bool, int]:
    if not settings.ENABLE_LOGIN_IP_BLOCK:
        return False, 0

    now = datetime.now(timezone.utc)
    with _LOCK:
        blocked_until = _BLOCKED_UNTIL.get(ip)
        if not blocked_until:
            return False, 0

        if blocked_until <= now:
            _BLOCKED_UNTIL.pop(ip, None)
            return False, 0

        remaining = int((blocked_until - now).total_seconds())
        return True, max(1, remaining)


def record_failed_login(ip: str) -> tuple[int, bool, int]:
    """Returns: (attempt_count, blocked_now, block_seconds_remaining)."""
    if not settings.ENABLE_LOGIN_IP_BLOCK:
        return 0, False, 0

    now = datetime.now(timezone.utc)

    with _LOCK:
        blocked_until = _BLOCKED_UNTIL.get(ip)
        if blocked_until and blocked_until > now:
            remaining = int((blocked_until - now).total_seconds())
            return settings.LOGIN_FAILURE_BLOCK_THRESHOLD, False, max(1, remaining)

        if blocked_until and blocked_until <= now:
            _BLOCKED_UNTIL.pop(ip, None)

        bucket = _prune_old_attempts(ip, now)
        bucket.append(now)
        attempt_count = len(bucket)

        if attempt_count >= settings.LOGIN_FAILURE_BLOCK_THRESHOLD:
            blocked_for = max(1, settings.LOGIN_FAILURE_BLOCK_SECONDS)
            _BLOCKED_UNTIL[ip] = now + timedelta(seconds=blocked_for)
            bucket.clear()
            return attempt_count, True, blocked_for

        return attempt_count, False, 0


def clear_failed_logins(ip: str) -> None:
    with _LOCK:
        _FAILED_LOGIN_BUCKETS.pop(ip, None)
        _BLOCKED_UNTIL.pop(ip, None)
