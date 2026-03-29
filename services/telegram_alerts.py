import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from urllib.error import HTTPError
from urllib import request as urllib_request

from core.config import settings


logger = logging.getLogger("zexplay.security.alerts")

_GEO_CACHE_TTL_SECONDS = 6 * 60 * 60
_GEO_CACHE_LOCK = Lock()
_IP_GEO_CACHE: dict[str, tuple[datetime, dict[str, str]]] = {}

_EVENT_META: dict[str, tuple[str, str]] = {
    "LOGIN_SUCCESS": ("LOW", "Successful Login"),
    "LOGIN_FAILED_BAD_PASSWORD": ("HIGH", "Failed Login (Bad Password)"),
    "LOGIN_FAILED_UNKNOWN_IDENTIFIER": ("HIGH", "Failed Login (Unknown Identifier)"),
    "LOGIN_BLOCKED_IP_HIT": ("CRITICAL", "Blocked IP Login Attempt"),
    "LOGIN_IP_BLOCKED": ("CRITICAL", "IP Block Triggered"),
}


def _telegram_enabled_state() -> tuple[bool, str]:
    if not settings.SECURITY_ALERTS_ENABLED:
        return False, "SECURITY_ALERTS_ENABLED is false"
    if not settings.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN is empty"
    if not settings.TELEGRAM_ALERT_CHAT_ID:
        return False, "TELEGRAM_ALERT_CHAT_ID is empty"
    return True, "ok"


def _safe_text(value: object, max_len: int = 240) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:max_len]


def _is_public_ip(ip: str) -> bool:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False

    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _extract_geo_profile(ip: str) -> dict[str, str]:
    if not settings.ENABLE_IP_GEO_LOOKUP:
        return {}
    if not _is_public_ip(ip):
        return {}

    timeout = settings.IP_GEO_LOOKUP_TIMEOUT_SECONDS
    if timeout <= 0:
        timeout = 2.0

    url = f"https://ipwho.is/{ip}"
    req = urllib_request.Request(
        url,
        headers={"User-Agent": "ZexPlaySecurityAlerts/1.0"},
        method="GET",
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.info("IP geo lookup failed for ip=%s error=%s", ip, exc)
        return {}

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        return {}

    if not isinstance(payload, dict) or payload.get("success") is False:
        return {}

    timezone_data = payload.get("timezone") if isinstance(payload.get("timezone"), dict) else {}
    connection_data = payload.get("connection") if isinstance(payload.get("connection"), dict) else {}

    city = payload.get("city")
    region = payload.get("region")
    country = payload.get("country")
    location_parts = [part for part in [city, region, country] if part]
    location = ", ".join(_safe_text(part, 64) for part in location_parts)

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    coordinates = ""
    maps = ""
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        coordinates = f"{latitude:.4f}, {longitude:.4f}"
        maps = f"https://maps.google.com/?q={latitude},{longitude}"

    values = {
        "geo_location": location,
        "geo_postal": payload.get("postal") or "",
        "geo_timezone": timezone_data.get("id") or "",
        "geo_utc_offset": timezone_data.get("utc") or "",
        "geo_coordinates": coordinates,
        "geo_maps": maps,
        "geo_isp": connection_data.get("isp") or "",
        "geo_org": connection_data.get("org") or "",
        "geo_domain": connection_data.get("domain") or "",
        "geo_asn": connection_data.get("asn") or "",
        "geo_continent": payload.get("continent") or "",
    }

    return {
        key: _safe_text(value, 200)
        for key, value in values.items()
        if value not in (None, "", "-")
    }


def _get_geo_profile(ip: str) -> dict[str, str]:
    if not ip:
        return {}

    now = datetime.now(timezone.utc)
    with _GEO_CACHE_LOCK:
        cached = _IP_GEO_CACHE.get(ip)
        if cached:
            expires_at, payload = cached
            if expires_at > now:
                return payload

    payload = _extract_geo_profile(ip)
    with _GEO_CACHE_LOCK:
        _IP_GEO_CACHE[ip] = (now + timedelta(seconds=_GEO_CACHE_TTL_SECONDS), payload)

    return payload


def _append_section(lines: list[str], title: str, rows: list[tuple[str, object]]) -> None:
    visible_rows: list[tuple[str, str]] = []
    for label, value in rows:
        if value is None:
            continue
        text = _safe_text(value, 300)
        if text in {"", "-"}:
            continue
        visible_rows.append((label, text))

    if not visible_rows:
        return

    lines.append("")
    lines.append(title)
    for label, text in visible_rows:
        lines.append(f"- {label}: {text}")


def _build_message(event: str, details: dict[str, object]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    severity, title = _EVENT_META.get(event, ("MEDIUM", event.replace("_", " ").title()))

    normalized: dict[str, str] = {}
    for key, value in details.items():
        if value is None:
            continue
        text = _safe_text(value, 260)
        if text in {"", "-"}:
            continue
        normalized[key] = text

    ip = normalized.get("ip", "")
    if ip:
        geo_profile = _get_geo_profile(ip)
        for key, value in geo_profile.items():
            normalized.setdefault(key, value)

    lines = [
        "=== ZexPlay Security Shield ===",
        f"Alert: {title}",
        f"Severity: {severity}",
        f"Event Code: {event}",
        f"Time (UTC): {timestamp}",
    ]

    _append_section(
        lines,
        "Account Context",
        [
            ("User ID", normalized.get("user_id")),
            ("Username", normalized.get("username")),
            ("Email", normalized.get("email")),
            ("Role", normalized.get("role")),
            ("Identifier", normalized.get("identifier")),
            ("Last User ID", normalized.get("last_user_id")),
        ],
    )

    _append_section(
        lines,
        "Threat Signal",
        [
            ("Attempts In Window", normalized.get("attempts_in_window")),
            ("Blocked Now", normalized.get("blocked_now")),
            ("Block Seconds", normalized.get("block_seconds")),
            ("Retry After Seconds", normalized.get("retry_after_seconds")),
            ("Reason", normalized.get("reason")),
        ],
    )

    _append_section(
        lines,
        "Network and Location",
        [
            ("Public IP", normalized.get("ip")),
            ("Location", normalized.get("geo_location")),
            ("Postal", normalized.get("geo_postal")),
            ("Timezone", normalized.get("geo_timezone")),
            ("UTC Offset", normalized.get("geo_utc_offset")),
            ("Coordinates", normalized.get("geo_coordinates")),
            ("Map", normalized.get("geo_maps")),
            ("ISP", normalized.get("geo_isp")),
            ("Organization", normalized.get("geo_org")),
            ("Domain", normalized.get("geo_domain")),
            ("ASN", normalized.get("geo_asn")),
            ("Continent", normalized.get("geo_continent")),
            ("Forwarded For", normalized.get("forwarded_for")),
            ("Real IP", normalized.get("real_ip")),
            ("Cloudflare IP", normalized.get("cf_connecting_ip")),
        ],
    )

    _append_section(
        lines,
        "Device and Request",
        [
            ("Device Fingerprint", normalized.get("device_fingerprint")),
            ("User Agent", normalized.get("user_agent")),
            ("Platform", normalized.get("platform")),
            ("Browser Hint", normalized.get("browser_hint")),
            ("Language", normalized.get("accept_language")),
            ("Origin", normalized.get("origin")),
            ("Referer", normalized.get("referer")),
        ],
    )

    message = "\n".join(lines)
    if len(message) <= 4096:
        return message
    return message[:4093] + "..."


def _send_message(message: str) -> None:
    enabled, reason = _telegram_enabled_state()
    if not enabled:
        logger.warning("Skipping Telegram alert delivery: %s", reason)
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_ALERT_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=settings.SECURITY_ALERT_TIMEOUT_SECONDS) as resp:
            raw_body = resp.read().decode("utf-8", errors="ignore")
            if resp.status >= 400:
                logger.warning("Telegram alert failed with HTTP status %s body=%s", resp.status, raw_body)
                return

            try:
                parsed = json.loads(raw_body) if raw_body else {}
            except Exception:
                parsed = {}

            if isinstance(parsed, dict) and parsed.get("ok") is False:
                logger.warning("Telegram alert rejected: %s", parsed)
            else:
                logger.info("Telegram security alert sent successfully")
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        logger.warning("Telegram alert HTTPError: status=%s body=%s", exc.code, body)
    except Exception as exc:
        logger.warning("Telegram alert delivery error: %s", exc)


def send_security_alert_async(event: str, details: dict[str, object]) -> None:
    enabled, reason = _telegram_enabled_state()
    if not enabled:
        logger.warning("Skipping security alert event=%s: %s", event, reason)
        return

    if event == "LOGIN_SUCCESS" and not settings.SECURITY_ALERT_ON_SUCCESS_LOGIN:
        return

    message = _build_message(event=event, details=details)
    Thread(target=_send_message, args=(message,), daemon=True).start()
