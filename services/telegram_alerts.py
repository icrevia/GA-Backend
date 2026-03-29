import json
import logging
from datetime import datetime, timezone
from threading import Thread
from urllib.error import HTTPError
from urllib import request as urllib_request

from core.config import settings


logger = logging.getLogger("zexplay.security.alerts")


def _telegram_enabled_state() -> tuple[bool, str]:
    if not settings.SECURITY_ALERTS_ENABLED:
        return False, "SECURITY_ALERTS_ENABLED is false"
    if not settings.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN is empty"
    if not settings.TELEGRAM_ALERT_CHAT_ID:
        return False, "TELEGRAM_ALERT_CHAT_ID is empty"
    return True, "ok"


def _build_message(event: str, details: dict[str, object]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "[ZexPlay Security Alert]",
        f"Event: {event}",
        f"Time (UTC): {timestamp}",
    ]

    for key, value in details.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")

    message = "\n".join(lines)
    return message[:4096]


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
