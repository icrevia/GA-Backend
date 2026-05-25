from __future__ import annotations

import asyncio
import logging
from typing import Any
import json
from threading import Thread
from urllib import request as urllib_request
from core.config import settings

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.websockets import manager
from models.user import User
from services.push_notifications import send_push, send_push_to_many

logger = logging.getLogger("GamerzAdda.support_notifications")


def _compact_preview(content: str | None, fallback: str = "New support message") -> str:
    text = (content or "").strip()
    if not text:
        return fallback
    if len(text) > 120:
        return f"{text[:117]}..."
    return text


def _send_tg_chat_alert_task(msg_data: dict[str, Any], user_id: int) -> None:
    bot_token = settings.CHAT_NOTI
    chat_ids_str = settings.TELEGRAM_ALERT_CHAT_ID
    if not bot_token or not chat_ids_str:
        return

    content = _compact_preview(msg_data.get("content"))
    media_type = msg_data.get("media_type")
    if media_type and media_type != "TEXT":
        content = f"[{media_type}] {content}"

    text = f"📩 *New Support Message*\nUser ID: `{user_id}`\nMessage: {content}"
    
    chat_ids = [cid.strip() for cid in chat_ids_str.split(",") if cid.strip()]
    for chat_id in chat_ids:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            req = urllib_request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=5.0) as resp:
                pass
        except Exception as e:
            logger.warning(f"Failed to send TG chat alert to {chat_id}: {e}")


async def _get_user_fcm_token(db: AsyncSession, user_id: int) -> str | None:
    result = await db.execute(select(User.fcm_token).where(User.id == user_id))
    token = result.scalar_one_or_none()
    return token or None


async def _get_admin_fcm_tokens(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(User.fcm_token)
        .where(User.role == "ADMIN")
        .where(User.fcm_token.isnot(None))
    )
    return [token for (token,) in result.all() if token]


async def notify_support_message(
    db: AsyncSession,
    thread_user_id: int,
    msg_data: dict[str, Any],
    sender_is_admin: bool,
    notify_via_push: bool = True,
) -> None:
    """Deliver a support chat message in real-time and fallback to push for offline recipients."""
    delivered_to_user = False
    if manager.is_user_online(thread_user_id):
        delivered_to_user = await manager.send_personal_message(msg_data, thread_user_id)
    await manager.broadcast_to_admins(msg_data)

    if not notify_via_push:
        return

    if sender_is_admin:
        if delivered_to_user:
            return

        token = await _get_user_fcm_token(db, thread_user_id)
        if not token:
            return

        title = "Support Reply"
        body = _compact_preview(msg_data.get("content"), fallback="New reply from support")
        data = {"type": "support_chat", "user_id": str(thread_user_id)}
        await asyncio.to_thread(send_push, token, title, body, data)
        return

    # User-sent messages should alert admins
    Thread(target=_send_tg_chat_alert_task, args=(msg_data, thread_user_id), daemon=True).start()

    if manager.is_admin_online():
        return

    admin_tokens = await _get_admin_fcm_tokens(db)
    if not admin_tokens:
        return

    title = "New Support Message"
    body = _compact_preview(msg_data.get("content"), fallback="A user sent a new support message")
    data = {"type": "admin_support_alert", "user_id": str(thread_user_id)}
    await asyncio.to_thread(send_push_to_many, admin_tokens, title, body, data)


async def notify_admin_escalation(
    db: AsyncSession,
    thread_user_id: int,
    preview: str,
    issue_type: str | None = None,
    user_name: str | None = None,
) -> None:
    event = {
        "type": "support_escalation",
        "user_id": thread_user_id,
        "preview": _compact_preview(preview),
        "issue_type": issue_type,
        "user_name": user_name,
    }
    await manager.broadcast_to_admins(event)

    if manager.is_admin_online():
        return

    admin_tokens = await _get_admin_fcm_tokens(db)
    if not admin_tokens:
        return

    title = "New Support Request"
    body = _compact_preview(preview, fallback="A user needs support")
    data = {
        "type": "admin_support_alert",
        "user_id": str(thread_user_id),
    }
    await asyncio.to_thread(send_push_to_many, admin_tokens, title, body, data)


async def notify_thread_state(
    db: AsyncSession,
    thread_user_id: int,
    event: dict[str, Any],
    notify_user_push: bool = False,
) -> None:
    delivered_to_user = False
    if manager.is_user_online(thread_user_id):
        delivered_to_user = await manager.send_personal_message(event, thread_user_id)
    await manager.broadcast_to_admins(event)

    if not notify_user_push or delivered_to_user:
        return

    token = await _get_user_fcm_token(db, thread_user_id)
    if not token:
        return

    event_type = str(event.get("type") or "")
    if event_type == "support_blocked":
        title = "Support Chat Blocked"
        body = _compact_preview(event.get("blocked_message"), fallback="Your support chat has been blocked by admin")
    elif event_type == "support_unblocked":
        title = "Support Chat Unblocked"
        body = "You can now message support again."
    elif event_type == "support_thread_updated" and bool(event.get("is_attended")) and not bool(event.get("is_ended")):
        title = "Support Agent Joined"
        body = "A support agent has joined your chat. You can continue messaging now."
    elif event_type == "support_thread_updated" and bool(event.get("is_ended")):
        title = "Support Chat Ended"
        body = _compact_preview(event.get("end_notice"), fallback="Your support chat has ended")
    else:
        title = "Support Chat Update"
        body = _compact_preview(event.get("end_notice"), fallback="Your support chat status has changed")

    data = {
        "type": "support_chat_state",
        "user_id": str(thread_user_id),
        "event_type": event_type,
    }
    await asyncio.to_thread(send_push, token, title, body, data)
