import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib import request as urllib_request
from urllib.error import HTTPError

from core.config import settings

logger = logging.getLogger("GamerzAdda.ledger.bot")

IST = timezone(timedelta(hours=5, minutes=30))
CALLBACK_PREFIX = "wd"
TELEGRAM_TIMEOUT_SECONDS = 8.0


def _clean_env_value(value: str | None) -> str:
    return str(value or "").strip().strip("\"'")


def get_ledger_bot_token() -> str:
    return _clean_env_value(settings.LEDGER_BOT)


def get_ledger_admin_chat_ids() -> list[str]:
    raw_chat_ids = _clean_env_value(settings.LEDGER_ADMINS)
    if not raw_chat_ids:
        return []
    return [
        chat_id.strip()
        for chat_id in raw_chat_ids.replace(";", ",").split(",")
        if chat_id.strip()
    ]


def is_ledger_admin_telegram_id(chat_id: str | int | None) -> bool:
    if chat_id is None:
        return False
    normalized_chat_id = str(chat_id).strip()
    if not normalized_chat_id:
        return False
    return normalized_chat_id in get_ledger_admin_chat_ids()


def _callback_signature(payload: str) -> str:
    secret = (settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def build_withdrawal_callback_data(action: str, transaction_id: int) -> str:
    action_code = str(action or "").strip().upper()
    if action_code not in {"A", "R"}:
        raise ValueError("Unsupported action for callback data")

    tx_id = int(transaction_id)
    payload = f"{action_code}:{tx_id}"
    return f"{CALLBACK_PREFIX}:{payload}:{_callback_signature(payload)}"


def parse_withdrawal_callback_data(callback_data: str) -> tuple[str, int] | None:
    raw = str(callback_data or "").strip()
    parts = raw.split(":")

    if len(parts) != 4:
        return None

    prefix, action_code, tx_id_raw, signature = parts
    if prefix != CALLBACK_PREFIX:
        return None

    action_code = action_code.upper()
    if action_code not in {"A", "R"}:
        return None

    try:
        tx_id = int(tx_id_raw)
    except (TypeError, ValueError):
        return None

    payload = f"{action_code}:{tx_id}"
    expected = _callback_signature(payload)
    if not hmac.compare_digest(signature, expected):
        return None

    return action_code, tx_id


def _telegram_api_request(method: str, payload: dict, timeout: float = TELEGRAM_TIMEOUT_SECONDS) -> dict | None:
    token = get_ledger_bot_token()
    if not token:
        return None

    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=max(timeout, 1.0)) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            parsed = json.loads(body) if body else {}
            if not isinstance(parsed, dict):
                logger.warning("Telegram %s returned non-dict payload", method)
                return None
            if parsed.get("ok") is False:
                logger.warning("Telegram %s rejected payload: %s", method, parsed)
                return None
            return parsed
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        logger.warning("Telegram %s HTTPError status=%s body=%s", method, exc.code, body)
    except Exception as exc:
        logger.warning("Telegram %s request failed: %s", method, exc)

    return None


def answer_callback_query(callback_query_id: str, text: str, show_alert: bool = False) -> None:
    if not callback_query_id:
        return

    _telegram_api_request(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": str(text or "").strip()[:180],
            "show_alert": bool(show_alert),
        },
        timeout=5.0,
    )


def edit_message_text(chat_id: str | int, message_id: int, text: str) -> None:
    _telegram_api_request(
        "editMessageText",
        {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": str(text or "").strip()[:4096],
            "disable_web_page_preview": True,
        },
        timeout=8.0,
    )


def _format_amount(value: Decimal | float | int) -> str:
    return f"{abs(float(value)):.2f}"


def _format_datetime_ist(value: datetime | None) -> str:
    if value is None:
        return "-"

    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).strftime("%d %b %Y %I:%M:%S %p IST")


def build_withdrawal_resolution_text(
    *,
    transaction_id: int,
    user_id: int,
    amount: Decimal | float | int,
    upi_id: str | None,
    status: str,
    actor_label: str,
    refunded_amount: Decimal | float | int = Decimal("0.00"),
) -> str:
    lines = [
        "WITHDRAWAL UPDATE",
        f"Transaction ID: {transaction_id}",
        f"User ID: {user_id}",
        f"Amount: Rs {_format_amount(amount)}",
        f"UPI ID: {upi_id or '-'}",
        f"Status: {status}",
        f"Action By: {actor_label}",
    ]

    if abs(float(refunded_amount)) > 0.0:
        lines.append(f"Refunded: Rs {_format_amount(refunded_amount)}")

    return "\n".join(lines)[:4096]


def send_withdrawal_request_to_admins(
    *,
    transaction_id: int,
    user_id: int,
    username: str,
    amount: Decimal | float | int,
    upi_id: str,
    reference_id: str | None,
    created_at: datetime | None,
    phone_number: str | None = None,
    freefire_id: str | None = None,
    withdrawal_fee: Decimal | float | int = Decimal("0.00"),
) -> int:
    token = get_ledger_bot_token()
    admin_chat_ids = get_ledger_admin_chat_ids()

    if not token:
        logger.info("Skipping ledger notification: LEDGER_BOT is empty")
        return 0

    if not admin_chat_ids:
        logger.info("Skipping ledger notification: LEDGER_ADMINS is empty")
        return 0

    lines = [
        "NEW WITHDRAWAL REQUEST",
        f"Transaction ID: {transaction_id}",
        f"Reference: {reference_id or '-'}",
        f"User ID: {user_id}",
        f"Username: {username or '-'}",
        f"Amount: Rs {_format_amount(amount)}",
        f"UPI ID: {upi_id or '-'}",
        f"Requested At: {_format_datetime_ist(created_at)}",
    ]

    if phone_number:
        lines.append(f"Phone: {phone_number}")
    if freefire_id:
        lines.append(f"Free Fire ID: {freefire_id}")
    if abs(float(withdrawal_fee)) > 0.0:
        lines.append(f"Processing Fee: Rs {_format_amount(withdrawal_fee)}")

    lines.append("")
    lines.append("Choose an action below.")

    text = "\n".join(lines)[:4096]

    try:
        approve_cb = build_withdrawal_callback_data("A", transaction_id)
        reject_cb = build_withdrawal_callback_data("R", transaction_id)
    except Exception:
        logger.exception("Failed to build callback payload for withdrawal tx=%s", transaction_id)
        return 0

    reply_markup = {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": approve_cb},
            {"text": "Reject", "callback_data": reject_cb},
        ]]
    }

    delivered = 0
    for chat_id in admin_chat_ids:
        response = _telegram_api_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
            timeout=10.0,
        )
        if isinstance(response, dict) and response.get("ok"):
            delivered += 1

    if delivered == 0:
        logger.warning("Withdrawal alert delivery failed for tx=%s", transaction_id)
    else:
        logger.info(
            "Withdrawal alert delivered for tx=%s to %s/%s admins",
            transaction_id,
            delivered,
            len(admin_chat_ids),
        )

    return delivered


def register_ledger_bot_webhook() -> bool:
    token = get_ledger_bot_token()
    if not token:
        logger.info("Skipping ledger webhook registration: LEDGER_BOT is empty")
        return False

    app_url = _clean_env_value(settings.APP_URL).rstrip("/")
    if not app_url:
        logger.info("Skipping ledger webhook registration: APP_URL is empty")
        return False

    webhook_url = f"{app_url}{settings.API_V1_STR}/ledger-bot/webhook"
    payload: dict[str, object] = {
        "url": webhook_url,
        "allowed_updates": ["callback_query"],
    }

    webhook_secret = _clean_env_value(settings.LEDGER_WEBHOOK_SECRET)
    if webhook_secret:
        payload["secret_token"] = webhook_secret

    response = _telegram_api_request("setWebhook", payload, timeout=12.0)
    if isinstance(response, dict) and response.get("ok"):
        logger.info("Ledger bot webhook configured at %s", webhook_url)
        return True

    logger.warning("Failed to configure ledger webhook at %s", webhook_url)
    return False
