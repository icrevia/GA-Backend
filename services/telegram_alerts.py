import json
import logging
from datetime import datetime, timezone
from threading import Thread
from urllib import request as urllib_request

from core.config import settings


logger = logging.getLogger("zexplay.security.alerts")


def _telegram_enabled() -> bool:
    return bool(
        settings.SECURITY_ALERTS_ENABLED
        and settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_ALERT_CHAT_ID
    )


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
    if not _telegram_enabled():
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
            if resp.status >= 400:
                logger.warning("Telegram alert failed with HTTP status %s", resp.status)
    except Exception as exc:
        logger.warning("Telegram alert delivery error: %s", exc)


def send_security_alert_async(event: str, details: dict[str, object]) -> None:
    if not _telegram_enabled():
        return

    if event == "LOGIN_SUCCESS" and not settings.SECURITY_ALERT_ON_SUCCESS_LOGIN:
        return

    message = _build_message(event=event, details=details)
    Thread(target=_send_message, args=(message,), daemon=True).start()
