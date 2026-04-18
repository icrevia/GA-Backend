from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.deps import get_current_user_async, get_user_for_support_async
from core.config import settings
from core.database import get_db
from core.security import decode_access_token
from core.websockets import manager
from models.support import ChatMessage, ChatSession
from models.user import User
from services.support_media import SupportMediaValidationError, store_support_media
from services.support_notifications import notify_admin_escalation, notify_support_message, notify_thread_state

logger = logging.getLogger("GamerzAdda.support")

MAX_SUPPORT_MESSAGE_LENGTH = 1000
MAX_ISSUE_TYPE_LENGTH = 120
DEFAULT_BLOCKED_MESSAGE = "Blocked by admin. Contact via WhatsApp support."
SESSION_STATUS_ACTIVE = "ACTIVE"
SESSION_STATUS_ENDED = "ENDED"

router = APIRouter()


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)
    issue_type: Optional[str] = Field(default=None, max_length=MAX_ISSUE_TYPE_LENGTH)
    is_issue_selection: bool = False


class SelectIssueRequest(BaseModel):
    issue_type: str = Field(min_length=1, max_length=MAX_ISSUE_TYPE_LENGTH)


class AdminReplyRequest(BaseModel):
    user_id: int
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)


class AdminStatusRequest(BaseModel):
    user_id: int


class AdminBlockRequest(BaseModel):
    user_id: int
    blocked_message: Optional[str] = Field(default=None, max_length=260)


class EndChatRequest(BaseModel):
    session_id: Optional[int] = None


def _utcnow_naive() -> datetime:
    return datetime.utcnow()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _normalize_issue_type(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    clean = raw_value.strip()
    if not clean:
        return None
    return clean[:MAX_ISSUE_TYPE_LENGTH]


def _normalize_blocked_message(raw_value: str | None) -> str:
    clean = (raw_value or "").strip()
    return clean or DEFAULT_BLOCKED_MESSAGE


def _support_whatsapp_url() -> str:
    digits = "".join(ch for ch in (settings.SUPPORT_WHATSAPP_NUMBER or "") if ch.isdigit())
    if not digits:
        digits = "917632932544"
    return f"https://wa.me/{digits}"


def _parse_form_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_message_content(raw_message: str) -> str:
    cleaned = raw_message.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(cleaned) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message must be <= {MAX_SUPPORT_MESSAGE_LENGTH} characters")
    return cleaned


def _normalize_caption(raw_caption: str) -> str:
    cleaned = (raw_caption or "").strip()
    if len(cleaned) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Caption must be <= {MAX_SUPPORT_MESSAGE_LENGTH} characters")
    return cleaned


def _serialize_msg(msg: ChatMessage) -> dict[str, Any]:
    return {
        "id": msg.id,
        "content": msg.content,
        "is_admin": bool(msg.is_admin),
        "is_read": bool(msg.is_read),
        "timestamp": _iso(msg.timestamp),
        "media_type": msg.media_type,
        "media_url": msg.media_url,
        "media_mime_type": msg.media_mime_type,
        "media_size_bytes": msg.media_size_bytes,
        "media_expires_at": _iso(msg.media_expires_at),
    }


def _message_event(msg: ChatMessage, thread_user_id: int) -> dict[str, Any]:
    payload = _serialize_msg(msg)
    payload["type"] = "chat_message"
    payload["user_id"] = thread_user_id
    return payload


def _thread_is_ended(meta: ChatSession) -> bool:
    return (meta.status or SESSION_STATUS_ACTIVE) == SESSION_STATUS_ENDED or meta.ended_at is not None


def _build_end_notice(ended_by_role: Optional[str], ended_by_name: Optional[str]) -> str:
    role = (ended_by_role or "").upper()
    if role == "ADMIN":
        actor = ended_by_name or "Admin"
        return f"This chat was closed by {actor}. Start a new chat from Support Hub if needed."
    if role == "USER":
        return "This chat was closed by you. Start a new chat from Support Hub if you need more help."
    return "This chat is ended. Start a new chat from Support Hub if needed."


def _media_label(media_type: Optional[str]) -> str:
    if media_type == "photo":
        return "Photo"
    if media_type == "audio":
        return "Voice message"
    if media_type == "video":
        return "Video"
    return "Attachment"


def _preview_for_message(message: ChatMessage) -> str:
    text = (message.content or "").strip()
    if text:
        if len(text) > 140:
            return f"{text[:137]}..."
        return text
    return _media_label(message.media_type)


def _issue_prompt(issue_type: str) -> str:
    return (
        f"Issue noted: {issue_type}. "
        "Please describe your issue in detail and our support team will assist you shortly."
    )


def _end_chat_message(ended_by_role: str, actor_name: Optional[str] = None) -> str:
    role = (ended_by_role or "").upper()
    if role == "USER":
        return "This chat was ended by you. Start a new chat from Support Hub if you need more help."
    actor = (actor_name or "Support").strip() or "Support"
    return f"This chat was ended by {actor}. Start a new chat from Support Hub if you still need help."


def _assert_admin(current_user: User) -> None:
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")


async def _resolve_username(db: AsyncSession, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    result = await db.execute(select(User.username).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _assert_admin_can_send(
    db: AsyncSession,
    meta: ChatSession,
    current_admin: User,
) -> None:
    if _thread_is_ended(meta):
        raise HTTPException(status_code=409, detail="Chat is already ended")

    if not meta.attended_by_admin_id:
        raise HTTPException(status_code=409, detail="Attend chat before sending messages")

    if meta.attended_by_admin_id != current_admin.id:
        attended_by_name = await _resolve_username(db, meta.attended_by_admin_id)
        actor = attended_by_name or "another admin"
        raise HTTPException(status_code=409, detail=f"Chat is currently attended by {actor}")


async def _get_or_create_thread_meta(db: AsyncSession, user_id: int) -> ChatSession:
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.id.desc())
        .limit(1)
    )
    meta = result.scalars().first()
    if meta:
        return meta

    meta = ChatSession(
        user_id=user_id,
        status=SESSION_STATUS_ACTIVE,
        requires_admin=False,
        issue_ack_sent=False,
        is_user_blocked=False,
        created_at=_utcnow_naive(),
    )
    db.add(meta)
    await db.flush()
    return meta


def _reopen_thread(meta: ChatSession) -> None:
    meta.status = SESSION_STATUS_ACTIVE
    meta.ended_at = None
    meta.ended_by_user_id = None
    meta.ended_by_role = None


async def _mark_messages_read(db: AsyncSession, thread_user_id: int, reader_is_admin: bool) -> int:
    now = _utcnow_naive()
    stmt = update(ChatMessage).where(
        ChatMessage.thread_user_id == thread_user_id,
        ChatMessage.is_read.is_(False),
    )
    if reader_is_admin:
        stmt = stmt.where(ChatMessage.is_admin.is_(False))
    else:
        stmt = stmt.where(ChatMessage.is_admin.is_(True))

    result = await db.execute(
        stmt.values(
            is_read=True,
            read_at=now,
            is_delivered=True,
            delivered_at=now,
        )
    )
    return int(result.rowcount or 0)


async def _build_thread_state_payload(
    db: AsyncSession,
    thread_user_id: int,
    meta: ChatSession,
    blocked_message_override: Optional[str] = None,
) -> dict[str, Any]:
    ended_by_name = await _resolve_username(db, meta.ended_by_user_id)
    is_ended = _thread_is_ended(meta)
    blocked_message = blocked_message_override if meta.is_user_blocked else None
    if meta.is_user_blocked and not blocked_message:
        blocked_message = DEFAULT_BLOCKED_MESSAGE

    return {
        "user_id": thread_user_id,
        "status": meta.status or SESSION_STATUS_ACTIVE,
        "requires_admin": bool(meta.requires_admin),
        "is_attended": meta.attended_by_admin_id is not None,
        "attended_by_admin_id": meta.attended_by_admin_id,
        "attended_at": _iso(meta.attended_at),
        "issue_type": meta.issue_type,
        "is_user_blocked": bool(meta.is_user_blocked),
        "blocked_message": blocked_message,
        "support_whatsapp_url": _support_whatsapp_url(),
        "is_ended": is_ended,
        "ended_at": _iso(meta.ended_at),
        "ended_by_role": meta.ended_by_role,
        "ended_by_name": ended_by_name,
        "end_notice": _build_end_notice(meta.ended_by_role, ended_by_name) if is_ended else None,
    }


async def _emit_thread_state(
    db: AsyncSession,
    thread_user_id: int,
    meta: ChatSession,
    event_type: str,
    blocked_message_override: Optional[str] = None,
    notify_user_push: bool = False,
) -> dict[str, Any]:
    payload = await _build_thread_state_payload(
        db,
        thread_user_id=thread_user_id,
        meta=meta,
        blocked_message_override=blocked_message_override,
    )
    event = {"type": event_type, **payload}
    await notify_thread_state(
        db,
        thread_user_id=thread_user_id,
        event=event,
        notify_user_push=notify_user_push,
    )
    return event


@router.websocket("/ws/{user_id}")
async def legacy_support_ws_endpoint(
    websocket: WebSocket,
    user_id: int,
    token: str = "",
    db: AsyncSession = Depends(get_db),
):
    if not token or token in {"null", "undefined", ""}:
        await websocket.close(code=1008)
        return

    try:
        payload = decode_access_token(token)
        token_uid = int(payload.get("sub"))
    except Exception:
        await websocket.close(code=1008)
        return

    role_result = await db.execute(select(User.role).where(User.id == token_uid))
    role = role_result.scalar_one_or_none() or "USER"
    is_admin = role == "ADMIN"
    if not is_admin and token_uid != user_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await manager.connect(token_uid, websocket, is_admin=is_admin)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        manager.disconnect(token_uid, websocket)


@router.get("/my-chat")
async def get_my_chat(
    since_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    meta = await _get_or_create_thread_meta(db, current_user.id)

    query = select(ChatMessage).where(ChatMessage.thread_user_id == current_user.id)
    if meta.user_cleared_at is not None:
        query = query.where(ChatMessage.timestamp > meta.user_cleared_at)
    if since_id is not None:
        query = query.where(ChatMessage.id > since_id)
    query = query.order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).limit(limit)

    result = await db.execute(query)
    messages = list(reversed(result.scalars().all()))

    await _mark_messages_read(db, current_user.id, reader_is_admin=False)
    await db.commit()

    ended_by_name = await _resolve_username(db, meta.ended_by_user_id)
    is_ended = _thread_is_ended(meta)

    return {
        "status": "success",
        "is_ended": is_ended,
        "ended_at": _iso(meta.ended_at),
        "ended_by_role": meta.ended_by_role,
        "ended_by_name": ended_by_name,
        "end_notice": _build_end_notice(meta.ended_by_role, ended_by_name) if is_ended else None,
        "issue_type": meta.issue_type,
        "is_user_blocked": bool(meta.is_user_blocked),
        "blocked_message": DEFAULT_BLOCKED_MESSAGE if meta.is_user_blocked else None,
        "support_whatsapp_url": _support_whatsapp_url(),
        "attended_by_admin_id": meta.attended_by_admin_id,
        "messages": [_serialize_msg(m) for m in messages],
    }


@router.post("/select-issue")
async def user_select_issue(
    req: SelectIssueRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    meta = await _get_or_create_thread_meta(db, current_user.id)
    if meta.is_user_blocked:
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)

    issue_type = _normalize_issue_type(req.issue_type)
    if issue_type is None:
        raise HTTPException(status_code=400, detail="Issue type cannot be empty")

    now = _utcnow_naive()
    if _thread_is_ended(meta):
        _reopen_thread(meta)

    # A fresh issue selection should wait for user details before escalating to admins.
    meta.issue_type = issue_type
    meta.requires_admin = False
    meta.attended_by_admin_id = None
    meta.attended_at = None
    meta.issue_ack_sent = True

    delivered = manager.is_user_online(current_user.id)
    prompt_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=current_user.id,
        sender_id=None,
        content=_issue_prompt(issue_type),
        timestamp=now,
        is_admin=True,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
    )
    db.add(prompt_msg)

    await db.commit()
    await db.refresh(prompt_msg)

    if delivered:
        await manager.send_personal_message(_message_event(prompt_msg, current_user.id), current_user.id)

    return {
        "status": "success",
        "issue_type": meta.issue_type,
        "message": _serialize_msg(prompt_msg),
    }


@router.post("/send")
async def user_send(
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    meta = await _get_or_create_thread_meta(db, current_user.id)
    if meta.is_user_blocked:
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)

    content = _normalize_message_content(req.message)
    now = _utcnow_naive()
    issue_type = _normalize_issue_type(req.issue_type)
    was_requires_admin = bool(meta.requires_admin)

    if _thread_is_ended(meta):
        _reopen_thread(meta)

    meta.requires_admin = True
    if issue_type is not None:
        meta.issue_type = issue_type
    if req.is_issue_selection:
        meta.issue_ack_sent = False

    delivered = manager.is_admin_online()
    new_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=current_user.id,
        sender_id=current_user.id,
        content=content,
        timestamp=now,
        is_admin=False,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
    )
    db.add(new_msg)

    await db.commit()
    await db.refresh(new_msg)

    msg_event = _message_event(new_msg, current_user.id)
    await notify_support_message(
        db,
        thread_user_id=current_user.id,
        msg_data=msg_event,
        sender_is_admin=False,
    )

    should_escalate = req.is_issue_selection or (not meta.attended_by_admin_id and not was_requires_admin)
    if should_escalate:
        await notify_admin_escalation(
            db,
            thread_user_id=current_user.id,
            preview=_preview_for_message(new_msg),
            issue_type=meta.issue_type,
            user_name=current_user.username,
        )

    await _emit_thread_state(
        db,
        thread_user_id=current_user.id,
        meta=meta,
        event_type="support_thread_updated",
    )

    return {"status": "success", "message": _serialize_msg(new_msg)}


@router.post("/upload")
async def user_upload(
    caption: str = Form(default=""),
    issue_type: Optional[str] = Form(default=None),
    is_issue_selection: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    meta = await _get_or_create_thread_meta(db, current_user.id)
    if meta.is_user_blocked:
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)

    cleaned_caption = _normalize_caption(caption)
    normalized_issue_type = _normalize_issue_type(issue_type)
    selection_flag = _parse_form_bool(is_issue_selection)
    was_requires_admin = bool(meta.requires_admin)

    try:
        media = await store_support_media(
            upload_file=file,
            owner_user_id=current_user.id,
            sender_role="USER",
        )
    except SupportMediaValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Support media upload failed for user_id=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Media upload failed")

    now = _utcnow_naive()
    if _thread_is_ended(meta):
        _reopen_thread(meta)

    meta.requires_admin = True
    if normalized_issue_type is not None:
        meta.issue_type = normalized_issue_type
    if selection_flag:
        meta.issue_ack_sent = False

    delivered = manager.is_admin_online()
    new_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=current_user.id,
        sender_id=current_user.id,
        content=cleaned_caption or _media_label(media.media_type),
        timestamp=now,
        is_admin=False,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
        media_type=media.media_type,
        media_url=media.public_url,
        media_path=media.relative_path,
        media_mime_type=media.mime_type,
        media_size_bytes=media.size_bytes,
        media_expires_at=media.expires_at,
    )
    db.add(new_msg)

    await db.commit()
    await db.refresh(new_msg)

    msg_event = _message_event(new_msg, current_user.id)
    await notify_support_message(
        db,
        thread_user_id=current_user.id,
        msg_data=msg_event,
        sender_is_admin=False,
    )

    should_escalate = selection_flag or (not meta.attended_by_admin_id and not was_requires_admin)
    if should_escalate:
        await notify_admin_escalation(
            db,
            thread_user_id=current_user.id,
            preview=_preview_for_message(new_msg),
            issue_type=meta.issue_type,
            user_name=current_user.username,
        )

    await _emit_thread_state(
        db,
        thread_user_id=current_user.id,
        meta=meta,
        event_type="support_thread_updated",
    )

    return {"status": "success", "message": _serialize_msg(new_msg)}


@router.post("/end")
async def user_end_chat(
    req: EndChatRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    req = req or EndChatRequest()
    meta = await _get_or_create_thread_meta(db, current_user.id)
    if req.session_id and meta.id and req.session_id != meta.id:
        raise HTTPException(status_code=409, detail="Chat session mismatch")

    now = _utcnow_naive()
    meta.status = SESSION_STATUS_ENDED
    meta.requires_admin = False
    meta.ended_at = now
    meta.ended_by_role = "USER"
    meta.ended_by_user_id = current_user.id

    await db.commit()

    event = await _emit_thread_state(
        db,
        thread_user_id=current_user.id,
        meta=meta,
        event_type="support_thread_updated",
        notify_user_push=False,
    )
    return {
        "status": "ended",
        "ended_at": event.get("ended_at"),
        "end_notice": event.get("end_notice"),
    }


@router.post("/clear-ended")
async def user_clear_ended_chat(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    meta = await _get_or_create_thread_meta(db, current_user.id)
    if not _thread_is_ended(meta):
        return {"status": "skipped", "reason": "chat_not_ended"}

    now = _utcnow_naive()
    meta.user_cleared_at = now
    await db.commit()

    return {"status": "cleared", "cleared_at": _iso(now)}


@router.get("/admin/threads")
async def get_admin_threads(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    thread_users_sq = select(ChatSession.user_id.label("user_id")).union(
        select(ChatMessage.thread_user_id.label("user_id"))
    ).subquery()

    latest_session_sq = (
        select(
            ChatSession.user_id.label("user_id"),
            func.max(ChatSession.id).label("session_id"),
        )
        .group_by(ChatSession.user_id)
        .subquery()
    )

    ranked_messages_sq = (
        select(
            ChatMessage.thread_user_id.label("thread_user_id"),
            ChatMessage.id.label("message_id"),
            ChatMessage.content.label("last_message"),
            ChatMessage.timestamp.label("last_timestamp"),
            ChatMessage.media_type.label("last_media_type"),
            func.row_number().over(
                partition_by=ChatMessage.thread_user_id,
                order_by=(ChatMessage.timestamp.desc(), ChatMessage.id.desc()),
            ).label("rn"),
        )
        .subquery()
    )

    last_message_sq = (
        select(
            ranked_messages_sq.c.thread_user_id,
            ranked_messages_sq.c.last_message,
            ranked_messages_sq.c.last_timestamp,
            ranked_messages_sq.c.last_media_type,
        )
        .where(ranked_messages_sq.c.rn == 1)
        .subquery()
    )

    unread_sq = (
        select(
            ChatMessage.thread_user_id.label("thread_user_id"),
            func.count(ChatMessage.id).label("unread_count"),
        )
        .where(
            ChatMessage.is_admin.is_(False),
            ChatMessage.is_read.is_(False),
        )
        .group_by(ChatMessage.thread_user_id)
        .subquery()
    )

    latest_session = aliased(ChatSession)
    ended_by_user = aliased(User)

    stmt = (
        select(
            thread_users_sq.c.user_id.label("thread_user_id"),
            latest_session.id.label("session_id"),
            latest_session.status.label("status"),
            latest_session.requires_admin.label("requires_admin"),
            latest_session.attended_by_admin_id.label("attended_by_admin_id"),
            latest_session.attended_at.label("attended_at"),
            latest_session.is_user_blocked.label("is_user_blocked"),
            latest_session.blocked_at.label("blocked_at"),
            latest_session.issue_type.label("issue_type"),
            latest_session.ended_at.label("ended_at"),
            latest_session.ended_by_role.label("ended_by_role"),
            latest_session.ended_by_user_id.label("ended_by_user_id"),
            latest_session.created_at.label("created_at"),
            User.id.label("user_id"),
            User.username.label("username"),
            User.email.label("email"),
            User.profile_pic.label("profile_pic"),
            ended_by_user.username.label("ended_by_name"),
            last_message_sq.c.last_message.label("last_message"),
            last_message_sq.c.last_timestamp.label("last_timestamp"),
            last_message_sq.c.last_media_type.label("last_media_type"),
            func.coalesce(unread_sq.c.unread_count, 0).label("unread_count"),
        )
        .select_from(thread_users_sq)
        .join(User, User.id == thread_users_sq.c.user_id)
        .outerjoin(latest_session_sq, latest_session_sq.c.user_id == thread_users_sq.c.user_id)
        .outerjoin(latest_session, latest_session.id == latest_session_sq.c.session_id)
        .outerjoin(ended_by_user, ended_by_user.id == latest_session.ended_by_user_id)
        .outerjoin(last_message_sq, last_message_sq.c.thread_user_id == thread_users_sq.c.user_id)
        .outerjoin(unread_sq, unread_sq.c.thread_user_id == thread_users_sq.c.user_id)
        .order_by(
            func.coalesce(latest_session.requires_admin, False).desc(),
            last_message_sq.c.last_timestamp.desc().nullslast(),
            func.coalesce(latest_session.created_at, last_message_sq.c.last_timestamp).desc().nullslast(),
        )
    )

    result = await db.execute(stmt)
    rows = result.mappings().all()

    threads: list[dict[str, Any]] = []
    for row in rows:
        thread_user_id = int(row["thread_user_id"])
        status = row["status"] or SESSION_STATUS_ACTIVE
        is_ended = status == SESSION_STATUS_ENDED or row["ended_at"] is not None
        unread = int(row["unread_count"] or 0)
        requires_admin = bool(row["requires_admin"]) or (unread > 0 and not is_ended)
        is_user_blocked = bool(row["is_user_blocked"])

        last_message = (row["last_message"] or "").strip()
        if not last_message and row["last_media_type"]:
            last_message = _media_label(row["last_media_type"])

        thread = {
            "id": thread_user_id,
            "session_id": row["session_id"],
            "user_id": thread_user_id,
            "user": {
                "id": row["user_id"],
                "username": row["username"],
                "email": row["email"],
                "profile_pic": row["profile_pic"],
            },
            "last_message": last_message,
            "last_timestamp": _iso(row["last_timestamp"]),
            "issue_type": row["issue_type"],
            "unread": unread,
            "requires_admin": requires_admin,
            "is_attended": row["attended_by_admin_id"] is not None,
            "attended_by_admin_id": row["attended_by_admin_id"],
            "attended_at": _iso(row["attended_at"]),
            "is_user_blocked": is_user_blocked,
            "blocked_message": DEFAULT_BLOCKED_MESSAGE if is_user_blocked else None,
            "status": status,
            "is_ended": is_ended,
            "ended_at": _iso(row["ended_at"]),
            "ended_by_role": row["ended_by_role"],
            "ended_by_name": row["ended_by_name"],
            "end_notice": _build_end_notice(row["ended_by_role"], row["ended_by_name"]) if is_ended else None,
        }
        threads.append(thread)

    return threads


@router.get("/admin/thread/{user_id}")
async def get_admin_thread_history(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    await _get_or_create_thread_meta(db, user_id)
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.thread_user_id == user_id)
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
    )
    messages = result.scalars().all()

    has_unread_user_messages = any((not m.is_admin) and (not m.is_read) for m in messages)
    if has_unread_user_messages:
        await _mark_messages_read(db, user_id, reader_is_admin=True)
        await db.commit()

    return [_serialize_msg(m) for m in messages]


@router.post("/admin/reply")
async def admin_reply(
    req: AdminReplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    target_exists = await db.execute(select(User.id).where(User.id == req.user_id))
    if target_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found")

    meta = await _get_or_create_thread_meta(db, req.user_id)
    await _assert_admin_can_send(db, meta, current_user)
    content = _normalize_message_content(req.message)
    now = _utcnow_naive()

    delivered = manager.is_user_online(req.user_id)
    new_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=req.user_id,
        sender_id=current_user.id,
        content=content,
        timestamp=now,
        is_admin=True,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
    )
    db.add(new_msg)

    meta.requires_admin = False
    meta.issue_ack_sent = True

    await db.commit()
    await db.refresh(new_msg)

    msg_event = _message_event(new_msg, req.user_id)
    await notify_support_message(
        db,
        thread_user_id=req.user_id,
        msg_data=msg_event,
        sender_is_admin=True,
    )
    await _emit_thread_state(
        db,
        thread_user_id=req.user_id,
        meta=meta,
        event_type="support_thread_updated",
    )

    return {"status": "success", "message": _serialize_msg(new_msg)}


@router.post("/admin/upload")
async def admin_upload(
    user_id: int = Form(...),
    caption: str = Form(default=""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    target_exists = await db.execute(select(User.id).where(User.id == user_id))
    if target_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found")

    meta = await _get_or_create_thread_meta(db, user_id)
    await _assert_admin_can_send(db, meta, current_user)
    cleaned_caption = _normalize_caption(caption)

    try:
        media = await store_support_media(
            upload_file=file,
            owner_user_id=user_id,
            sender_role="ADMIN",
        )
    except SupportMediaValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Admin media upload failed for thread_user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Media upload failed")

    now = _utcnow_naive()

    meta.requires_admin = False
    meta.issue_ack_sent = True

    delivered = manager.is_user_online(user_id)
    new_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=user_id,
        sender_id=current_user.id,
        content=cleaned_caption or _media_label(media.media_type),
        timestamp=now,
        is_admin=True,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
        media_type=media.media_type,
        media_url=media.public_url,
        media_path=media.relative_path,
        media_mime_type=media.mime_type,
        media_size_bytes=media.size_bytes,
        media_expires_at=media.expires_at,
    )
    db.add(new_msg)

    await db.commit()
    await db.refresh(new_msg)

    msg_event = _message_event(new_msg, user_id)
    await notify_support_message(
        db,
        thread_user_id=user_id,
        msg_data=msg_event,
        sender_is_admin=True,
    )
    await _emit_thread_state(
        db,
        thread_user_id=user_id,
        meta=meta,
        event_type="support_thread_updated",
    )

    return {"status": "success", "message": _serialize_msg(new_msg)}


@router.post("/admin/attend")
async def admin_attend(
    req: AdminStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    meta = await _get_or_create_thread_meta(db, req.user_id)
    if _thread_is_ended(meta):
        raise HTTPException(status_code=409, detail="Chat is already ended")

    now = _utcnow_naive()
    meta.attended_by_admin_id = current_user.id
    meta.attended_at = now
    meta.requires_admin = False
    meta.issue_ack_sent = True

    await db.commit()
    await _emit_thread_state(
        db,
        thread_user_id=req.user_id,
        meta=meta,
        event_type="support_thread_updated",
        notify_user_push=True,
    )
    return {"status": "attended"}


@router.post("/admin/end")
async def admin_end(
    req: AdminStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    meta = await _get_or_create_thread_meta(db, req.user_id)
    now = _utcnow_naive()
    meta.status = SESSION_STATUS_ENDED
    meta.requires_admin = False
    meta.ended_at = now
    meta.ended_by_role = "ADMIN"
    meta.ended_by_user_id = current_user.id

    delivered = manager.is_user_online(req.user_id)
    ended_msg = ChatMessage(
        session_id=meta.id,
        thread_user_id=req.user_id,
        sender_id=current_user.id,
        content=_end_chat_message("ADMIN", current_user.username),
        timestamp=now,
        is_admin=True,
        is_delivered=delivered,
        delivered_at=now if delivered else None,
    )
    db.add(ended_msg)

    await db.commit()
    await db.refresh(ended_msg)

    await notify_support_message(
        db,
        thread_user_id=req.user_id,
        msg_data=_message_event(ended_msg, req.user_id),
        sender_is_admin=True,
    )

    event = await _emit_thread_state(
        db,
        thread_user_id=req.user_id,
        meta=meta,
        event_type="support_thread_updated",
        notify_user_push=False,
    )
    return {
        "status": "ended",
        "ended_at": event.get("ended_at"),
        "end_notice": event.get("end_notice"),
    }


@router.post("/admin/block")
async def admin_block(
    req: AdminBlockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    meta = await _get_or_create_thread_meta(db, req.user_id)
    meta.is_user_blocked = True
    meta.blocked_by_admin_id = current_user.id
    meta.blocked_at = _utcnow_naive()
    meta.requires_admin = False

    await db.commit()

    blocked_message = _normalize_blocked_message(req.blocked_message)
    event = await _emit_thread_state(
        db,
        thread_user_id=req.user_id,
        meta=meta,
        event_type="support_blocked",
        blocked_message_override=blocked_message,
        notify_user_push=True,
    )
    return {
        "status": "blocked",
        "blocked_message": event.get("blocked_message") or blocked_message,
    }


@router.post("/admin/unblock")
async def admin_unblock(
    req: AdminStatusRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    _assert_admin(current_user)

    meta = await _get_or_create_thread_meta(db, req.user_id)
    meta.is_user_blocked = False
    meta.blocked_by_admin_id = None
    meta.blocked_at = None

    await db.commit()

    await _emit_thread_state(
        db,
        thread_user_id=req.user_id,
        meta=meta,
        event_type="support_unblocked",
        notify_user_push=True,
    )
    return {"status": "unblocked"}
