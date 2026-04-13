from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import aliased
from pydantic import BaseModel, Field
from typing import List, Tuple
from core.database import get_db
from api.deps import get_current_user_async, get_user_for_support_async
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from core.config import settings
from services.support_media import SupportMediaValidationError, store_support_media
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("GamerzAdda.support")
IST = timezone(timedelta(hours=5, minutes=30))
MAX_SUPPORT_MESSAGE_LENGTH = 1000
MAX_ISSUE_TYPE_LENGTH = 120
AUTO_REPLY_TEXT = "Please wait, an admin will join you shortly to assist."
DEFAULT_BLOCKED_MESSAGE = "Blocked by admin. Contact via WhatsApp support."
SESSION_STATUS_ACTIVE = "ACTIVE"
SESSION_STATUS_ENDED = "ENDED"


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _normalize_issue_type(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    clean = raw_value.strip()
    if not clean:
        return None
    return clean[:MAX_ISSUE_TYPE_LENGTH]


def _support_whatsapp_url() -> str:
    digits = "".join(ch for ch in (settings.SUPPORT_WHATSAPP_NUMBER or "") if ch.isdigit())
    if not digits:
        digits = "917632932544"
    return f"https://wa.me/{digits}"


router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────

class AdminReplyRequest(BaseModel):
    session_id: int
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)


class AdminAttendRequest(BaseModel):
    session_id: int


class AdminBlockRequest(BaseModel):
    session_id: int


class AdminUnblockRequest(BaseModel):
    session_id: int


class AdminEndChatRequest(BaseModel):
    session_id: int


class EndChatRequest(BaseModel):
    session_id: int | None = None


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)
    issue_type: str | None = Field(default=None, max_length=MAX_ISSUE_TYPE_LENGTH)
    is_issue_selection: bool = False


async def _get_or_create_user_support_session(db: AsyncSession, user_id: int) -> Tuple[ChatSession, List[ChatSession]]:
    """Returns a stable primary session plus all sessions for a user.

    Some users may have duplicate sessions created over time (network races/reinstalls).
    We keep writing to a deterministic primary session and read history from all sessions.
    """
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
    )
    sessions = list(sessions_result.scalars().all())

    if sessions:
        active_session = next(
            (
                s for s in reversed(sessions)
                if (s.status or SESSION_STATUS_ACTIVE).upper() != SESSION_STATUS_ENDED
            ),
            None,
        )
        return (active_session or sessions[-1]), sessions

    session = ChatSession(user_id=user_id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session, [session]


async def _mark_user_requires_admin(db: AsyncSession, user_id: int, issue_type: str | None = None) -> None:
    values = {
        "status": SESSION_STATUS_ACTIVE,
        "ended_at": None,
        "ended_by_user_id": None,
        "ended_by_role": None,
        "requires_admin": True,
        "attended_by_admin_id": None,
        "attended_at": None,
    }
    if issue_type:
        values["issue_type"] = issue_type

    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(**values)
    )


async def _mark_user_attended(db: AsyncSession, user_id: int, admin_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(
            requires_admin=False,
            attended_by_admin_id=admin_id,
            attended_at=now_ist(),
        )
    )


async def _mark_user_blocked(db: AsyncSession, user_id: int, admin_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(
            is_user_blocked=True,
            blocked_by_admin_id=admin_id,
            blocked_at=now_ist(),
            requires_admin=False,
            attended_by_admin_id=admin_id,
            attended_at=now_ist(),
        )
    )


async def _mark_user_unblocked(db: AsyncSession, user_id: int, admin_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(
            is_user_blocked=False,
            blocked_by_admin_id=None,
            blocked_at=None,
            status=SESSION_STATUS_ACTIVE,
            requires_admin=False,
            attended_by_admin_id=admin_id,
            attended_at=now_ist(),
        )
    )


async def _mark_user_chat_ended(
    db: AsyncSession,
    user_id: int,
    ended_by_user_id: int,
    ended_by_role: str,
) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .where(ChatSession.status != SESSION_STATUS_ENDED)
        .values(
            status=SESSION_STATUS_ENDED,
            ended_at=now_ist(),
            ended_by_user_id=ended_by_user_id,
            ended_by_role=ended_by_role,
            requires_admin=False,
            issue_ack_sent=False,
        )
    )


async def _mark_user_messages_read(db: AsyncSession, user_session_ids: list[int]) -> None:
    if not user_session_ids:
        return
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.session_id.in_(user_session_ids))
        .where(ChatMessage.is_admin.is_(False))
        .where(ChatMessage.is_read.is_(False))
        .values(is_read=True)
    )


async def _resolve_admin_name(db: AsyncSession, admin_id: int | None) -> str | None:
    if not admin_id:
        return None
    result = await db.execute(select(User.username).where(User.id == admin_id))
    return result.scalar_one_or_none()


def _build_end_notice(ended_by_role: str | None, ended_by_name: str | None) -> str:
    role = (ended_by_role or "").upper()
    if role == "ADMIN":
        if ended_by_name:
            return f"Chat ended by admin ({ended_by_name})."
        return "Chat ended by admin."
    if role == "USER":
        return "You ended this chat."
    return "This chat is ended."


def _caption_or_placeholder(caption: str | None, media_type: str | None = None) -> str:
    clean_caption = (caption or "").strip()
    if clean_caption:
        return clean_caption[:MAX_SUPPORT_MESSAGE_LENGTH]

    if media_type == "photo":
        return "Photo"
    if media_type == "video":
        return "Video"
    return ""


def _serialize_chat_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "content": message.content,
        "is_admin": bool(message.is_admin),
        "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        "media_type": message.media_type,
        "media_url": message.media_url,
        "media_mime_type": message.media_mime_type,
        "media_size_bytes": message.media_size_bytes,
        "media_expires_at": message.media_expires_at.isoformat() if message.media_expires_at else None,
    }


def _chat_message_event(
    message: ChatMessage,
    session_id: int,
    user_id: int,
    issue_type: str | None = None,
) -> dict:
    payload = {
        "type": "chat_message",
        "session_id": session_id,
        "user_id": user_id,
    }
    payload.update(_serialize_chat_message(message))
    if issue_type:
        payload["issue_type"] = issue_type
    return payload


# ─────────────────────────────────────────────────────────────────
# WebSocket — requires JWT token, verifies ownership
# ─────────────────────────────────────────────────────────────────

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, token: str = ""):
    """Support WebSocket — authenticated, user can only connect as themselves."""
    from jose import jwt, JWTError

    if not token or token in ("null", "undefined", ""):
        await websocket.close(code=1008)
        return

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        token_uid = int(payload.get("sub", -1))
    except (JWTError, ValueError):
        await websocket.close(code=1008)
        return

    if token_uid != user_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await manager.connect(user_id, websocket)
    logger.info("Support WS connected: user_id=%s", user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        logger.info("Support WS disconnected: user_id=%s", user_id)


# ─────────────────────────────────────────────────────────────────
# Session & message endpoints
# ─────────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/messages", response_model=List[dict])
async def get_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    user_session_ids_result = await db.execute(select(ChatSession.id).where(ChatSession.user_id == session.user_id))
    user_session_ids = [sid for (sid,) in user_session_ids_result.all()]
    if not user_session_ids:
        user_session_ids = [session_id]

    messages_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id.in_(user_session_ids))
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
    )
    messages = messages_result.scalars().all()

    await _mark_user_messages_read(db, user_session_ids)
    await db.commit()

    return [_serialize_chat_message(m) for m in messages]


@router.get("/sessions", response_model=List[dict])
async def get_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    attended_admin = aliased(User)
    blocked_admin = aliased(User)
    ended_by_user = aliased(User)

    latest_message_sq = (
        select(
            ChatMessage.session_id.label("session_id"),
            ChatMessage.content.label("content"),
            ChatMessage.timestamp.label("timestamp"),
        )
        .distinct(ChatMessage.session_id)
        .order_by(ChatMessage.session_id, ChatMessage.timestamp.desc(), ChatMessage.id.desc())
        .subquery()
    )

    unread_sq = (
        select(
            ChatMessage.session_id.label("session_id"),
            func.count(ChatMessage.id).label("unread"),
        )
        .where(ChatMessage.is_admin.is_(False))
        .where(ChatMessage.is_read.is_(False))
        .group_by(ChatMessage.session_id)
        .subquery()
    )

    rows_result = await db.execute(
        select(
            ChatSession.id.label("session_id"),
            ChatSession.user_id.label("user_id"),
            ChatSession.created_at.label("created_at"),
            ChatSession.requires_admin.label("requires_admin"),
            ChatSession.attended_at.label("attended_at"),
            ChatSession.attended_by_admin_id.label("attended_by_admin_id"),
            ChatSession.issue_type.label("issue_type"),
            ChatSession.is_user_blocked.label("is_user_blocked"),
            ChatSession.blocked_at.label("blocked_at"),
            ChatSession.blocked_by_admin_id.label("blocked_by_admin_id"),
            ChatSession.status.label("status"),
            ChatSession.ended_at.label("ended_at"),
            ChatSession.ended_by_user_id.label("ended_by_user_id"),
            ChatSession.ended_by_role.label("ended_by_role"),
            User.username.label("username"),
            User.email.label("email"),
            attended_admin.username.label("attended_by_admin_name"),
            blocked_admin.username.label("blocked_by_admin_name"),
            ended_by_user.username.label("ended_by_name"),
            latest_message_sq.c.content.label("last_message"),
            latest_message_sq.c.timestamp.label("last_timestamp"),
            func.coalesce(unread_sq.c.unread, 0).label("unread"),
        )
        .join(User, User.id == ChatSession.user_id)
        .outerjoin(attended_admin, attended_admin.id == ChatSession.attended_by_admin_id)
        .outerjoin(blocked_admin, blocked_admin.id == ChatSession.blocked_by_admin_id)
        .outerjoin(ended_by_user, ended_by_user.id == ChatSession.ended_by_user_id)
        .outerjoin(
            latest_message_sq,
            latest_message_sq.c.session_id == ChatSession.id,
        )
        .outerjoin(unread_sq, unread_sq.c.session_id == ChatSession.id)
        .order_by(
            ChatSession.status == "ACTIVE",  # Active sessions first
            func.coalesce(latest_message_sq.c.timestamp, ChatSession.created_at).desc()
        )
    )
    rows = rows_result.all()

    return [
        {
            "id": row.session_id,
            "user_id": row.user_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "user": {
                "username": row.username,
                "email": row.email,
            },
            "last_message": row.last_message or "No messages yet",
            "last_timestamp": (
                (row.last_timestamp or row.created_at).isoformat()
                if (row.last_timestamp or row.created_at)
                else None
            ),
            "issue_type": row.issue_type,
            "status": (row.status or SESSION_STATUS_ACTIVE).upper(),
            "is_ended": (row.status or SESSION_STATUS_ACTIVE).upper() == SESSION_STATUS_ENDED,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "ended_by_user_id": row.ended_by_user_id,
            "ended_by_role": row.ended_by_role,
            "ended_by_name": row.ended_by_name,
            "end_notice": _build_end_notice(row.ended_by_role, row.ended_by_name) if row.ended_at else None,
            "requires_admin": bool(row.requires_admin),
            "is_attended": bool(row.attended_by_admin_id),
            "attended_at": row.attended_at.isoformat() if row.attended_at else None,
            "attended_by_admin_id": row.attended_by_admin_id,
            "attended_by_admin_name": row.attended_by_admin_name,
            "is_user_blocked": bool(row.is_user_blocked),
            "blocked_at": row.blocked_at.isoformat() if row.blocked_at else None,
            "blocked_by_admin_id": row.blocked_by_admin_id,
            "blocked_by_admin_name": row.blocked_by_admin_name,
            "blocked_message": DEFAULT_BLOCKED_MESSAGE if row.is_user_blocked else None,
            "support_whatsapp_url": _support_whatsapp_url(),
            "unread": int(row.unread or 0),
        }
        for row in rows
    ]


@router.get("/my-chat")
async def get_my_chat(
    since_id: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    session_ids = [s.id for s in all_sessions]

    message_query = select(ChatMessage).where(ChatMessage.session_id.in_(session_ids))
    if since_id > 0:
        message_query = message_query.where(ChatMessage.id > since_id)

    messages_result = await db.execute(
        message_query.order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
    )
    messages = messages_result.scalars().all()

    is_user_blocked = bool(session.is_user_blocked)
    attended_by_admin_name = await _resolve_admin_name(db, session.attended_by_admin_id)
    blocked_by_admin_name = await _resolve_admin_name(db, session.blocked_by_admin_id)
    ended_by_name = await _resolve_admin_name(db, session.ended_by_user_id)
    status_value = (session.status or SESSION_STATUS_ACTIVE).upper()
    is_ended = status_value == SESSION_STATUS_ENDED

    return {
        "session_id": session.id,
        "status": status_value,
        "is_ended": is_ended,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "ended_by_user_id": session.ended_by_user_id,
        "ended_by_role": session.ended_by_role,
        "ended_by_name": ended_by_name,
        "end_notice": _build_end_notice(session.ended_by_role, ended_by_name) if session.ended_at else None,
        "issue_type": session.issue_type,
        "requires_admin": bool(session.requires_admin),
        "is_attended": bool(session.attended_by_admin_id),
        "attended_by_admin_id": session.attended_by_admin_id,
        "attended_by_admin_name": attended_by_admin_name,
        "is_user_blocked": is_user_blocked,
        "blocked_by_admin_id": session.blocked_by_admin_id,
        "blocked_by_admin_name": blocked_by_admin_name,
        "blocked_message": DEFAULT_BLOCKED_MESSAGE if is_user_blocked else None,
        "support_whatsapp_url": _support_whatsapp_url(),
        "messages": [_serialize_chat_message(m) for m in messages],
    }


@router.post("/send")
async def send_message(
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    clean_message = body.message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(clean_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    if any(bool(s.is_user_blocked) for s in all_sessions):
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)

    if body.is_issue_selection:
        issue_type = _normalize_issue_type(body.issue_type)
        await _mark_user_requires_admin(db, session.user_id, issue_type=issue_type)
        
        auto_reply_sent = False
        if issue_type and not bool(session.issue_ack_sent):
            await db.execute(
                update(ChatSession)
                .where(ChatSession.user_id == session.user_id)
                .values(issue_ack_sent=True)
            )
            auto_reply_sent = True
            
        await db.commit()
        
        if auto_reply_sent:
            auto_reply_data = {
                "type": "chat_message",
                "session_id": session.id,
                "user_id": session.user_id,
                "id": -abs(current_user.id * 1000), # Fake ID
                "content": AUTO_REPLY_TEXT,
                "is_admin": True,
                "timestamp": now_ist().isoformat()
            }
            await manager.send_personal_message(auto_reply_data, session.user_id)
            
        return {
            "status": "success",
            "issue_type": issue_type,
            "auto_reply_sent": auto_reply_sent,
        }

    # Normal message flow
    issue_type = _normalize_issue_type(body.issue_type)
    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=False,
    )
    db.add(new_msg)

    await _mark_user_requires_admin(db, session.user_id, issue_type=issue_type)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = _chat_message_event(
        message=new_msg,
        session_id=session.id,
        user_id=session.user_id,
        issue_type=issue_type,
    )
    if not msg_data.get("timestamp"):
        msg_data["timestamp"] = now_ist().isoformat()

    await manager.send_personal_message(msg_data, session.user_id)
    await manager.broadcast_to_admins(msg_data)

    escalation_data = {
        "type": "support_escalation",
        "session_id": session.id,
        "user_id": session.user_id,
        "issue_type": issue_type,
        "preview": clean_message,
        "timestamp": msg_data["timestamp"],
    }
    await manager.broadcast_to_admins(escalation_data)

    return {
        "status": "success",
        "issue_type": issue_type,
        "auto_reply_sent": False,
    }


@router.post("/upload")
async def upload_media_message(
    file: UploadFile = File(...),
    caption: str = Form(default=""),
    issue_type: str | None = Form(default=None),
    is_issue_selection: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    clean_caption = (caption or "").strip()
    if len(clean_caption) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Caption is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    if any(bool(s.is_user_blocked) for s in all_sessions):
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)

    try:
        stored_media = await store_support_media(
            upload_file=file,
            owner_user_id=current_user.id,
            sender_role="user",
        )
    except SupportMediaValidationError as validation_error:
        raise HTTPException(status_code=400, detail=str(validation_error)) from validation_error
    except HTTPException:
        raise
    except Exception:
        logger.exception("Support media upload failed for user_id=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Unable to upload media right now")

    normalized_issue_type = _normalize_issue_type(issue_type)
    message_content = _caption_or_placeholder(clean_caption, stored_media.media_type)

    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=message_content,
        is_admin=False,
        media_type=stored_media.media_type,
        media_url=stored_media.public_url,
        media_path=stored_media.relative_path,
        media_mime_type=stored_media.mime_type,
        media_size_bytes=stored_media.size_bytes,
        media_expires_at=stored_media.expires_at,
    )
    db.add(new_msg)

    await _mark_user_requires_admin(db, session.user_id, issue_type=normalized_issue_type)

    auto_reply_msg = None
    if is_issue_selection and normalized_issue_type and not bool(session.issue_ack_sent):
        await db.execute(
            update(ChatSession)
            .where(ChatSession.user_id == session.user_id)
            .values(issue_ack_sent=True)
        )
        auto_reply_msg = ChatMessage(
            session_id=session.id,
            sender_id=current_user.id,
            content=AUTO_REPLY_TEXT,
            is_admin=True,
        )
        db.add(auto_reply_msg)

    await db.commit()
    await db.refresh(new_msg)
    if auto_reply_msg is not None:
        await db.refresh(auto_reply_msg)

    msg_data = _chat_message_event(
        message=new_msg,
        session_id=session.id,
        user_id=session.user_id,
        issue_type=normalized_issue_type,
    )
    if not msg_data.get("timestamp"):
        msg_data["timestamp"] = now_ist().isoformat()

    await manager.send_personal_message(msg_data, session.user_id)
    await manager.broadcast_to_admins(msg_data)

    escalation_data = {
        "type": "support_escalation",
        "session_id": session.id,
        "user_id": session.user_id,
        "issue_type": normalized_issue_type,
        "preview": message_content,
        "timestamp": msg_data["timestamp"],
    }
    await manager.broadcast_to_admins(escalation_data)

    if auto_reply_msg is not None:
        auto_reply_data = _chat_message_event(
            message=auto_reply_msg,
            session_id=session.id,
            user_id=session.user_id,
        )
        if not auto_reply_data.get("timestamp"):
            auto_reply_data["timestamp"] = now_ist().isoformat()
        await manager.send_personal_message(auto_reply_data, session.user_id)
        await manager.broadcast_to_admins(auto_reply_data)

    return {
        "status": "success",
        "issue_type": normalized_issue_type,
        "auto_reply_sent": auto_reply_msg is not None,
        "message": _serialize_chat_message(new_msg),
    }


@router.post("/log-call")
async def log_call(
    type: str = Query(...),
    duration: str = Query(None),
    user_id: int = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    logger.info(
        "Ignoring deprecated support call event: type=%s duration=%s actor_user_id=%s target_user_id=%s",
        type,
        duration,
        current_user.id,
        user_id,
    )
    return {
        "status": "ignored",
        "message": "Call events are deprecated. Use text chat only.",
    }


@router.post("/end")
async def end_chat_by_user(
    request: EndChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async),
):
    primary_session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    target_session = primary_session
    if request.session_id:
        target_session = next((s for s in all_sessions if s.id == request.session_id), primary_session)

    await _mark_user_chat_ended(
        db,
        user_id=current_user.id,
        ended_by_user_id=current_user.id,
        ended_by_role="USER",
    )
    await db.commit()

    event = {
        "type": "support_session_ended",
        "session_id": target_session.id,
        "user_id": current_user.id,
        "ended_by_role": "USER",
        "ended_by_user_id": current_user.id,
        "ended_by_name": current_user.username,
        "end_notice": _build_end_notice("USER", current_user.username),
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(event, current_user.id)
    await manager.broadcast_to_admins(event)

    return {
        "status": "success",
        "session_id": target_session.id,
        "ended_by_role": "USER",
    }


@router.post("/admin/end")
async def end_chat_by_admin(
    request: AdminEndChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await _mark_user_chat_ended(
        db,
        user_id=session.user_id,
        ended_by_user_id=current_user.id,
        ended_by_role="ADMIN",
    )
    await db.commit()

    event = {
        "type": "support_session_ended",
        "session_id": request.session_id,
        "user_id": session.user_id,
        "ended_by_role": "ADMIN",
        "ended_by_user_id": current_user.id,
        "ended_by_name": current_user.username,
        "end_notice": _build_end_notice("ADMIN", current_user.username),
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(event, session.user_id)
    await manager.broadcast_to_admins(event)

    return {
        "status": "success",
        "session_id": request.session_id,
        "user_id": session.user_id,
        "ended_by_role": "ADMIN",
    }


@router.post("/admin/attend")
async def admin_attend(
    request: AdminAttendRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if (session.status or SESSION_STATUS_ACTIVE).upper() == SESSION_STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Chat is ended. Wait for user to reopen it.")
    if session.is_user_blocked:
        raise HTTPException(status_code=400, detail="User is already blocked")

    user_session_ids_result = await db.execute(select(ChatSession.id).where(ChatSession.user_id == session.user_id))
    user_session_ids = [sid for (sid,) in user_session_ids_result.all()]

    await _mark_user_attended(db, session.user_id, current_user.id)
    await _mark_user_messages_read(db, user_session_ids)
    await db.commit()

    event = {
        "type": "support_session_attended",
        "session_id": request.session_id,
        "user_id": session.user_id,
        "attended_by_admin_id": current_user.id,
        "attended_by_admin_name": current_user.username,
        "timestamp": now_ist().isoformat(),
    }
    await manager.broadcast_to_admins(event)

    return {
        "status": "success",
        "attended_by_admin_id": current_user.id,
        "attended_by_admin_name": current_user.username,
    }


@router.post("/admin/block")
async def admin_block_user(
    request: AdminBlockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await _mark_user_blocked(db, session.user_id, current_user.id)
    await db.commit()

    blocked_notice = {
        "type": "support_blocked",
        "user_id": session.user_id,
        "session_id": request.session_id,
        "blocked_message": DEFAULT_BLOCKED_MESSAGE,
        "support_whatsapp_url": _support_whatsapp_url(),
        "blocked_by_admin_id": current_user.id,
        "blocked_by_admin_name": current_user.username,
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(blocked_notice, session.user_id)
    await manager.broadcast_to_admins(blocked_notice)

    return {
        "status": "success",
        "user_id": session.user_id,
        "blocked_message": DEFAULT_BLOCKED_MESSAGE,
        "support_whatsapp_url": _support_whatsapp_url(),
    }


@router.post("/admin/unblock")
async def admin_unblock_user(
    request: AdminUnblockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await _mark_user_unblocked(db, session.user_id, current_user.id)
    await db.commit()

    unblocked_notice = {
        "type": "support_unblocked",
        "user_id": session.user_id,
        "session_id": request.session_id,
        "unblocked_by_admin_id": current_user.id,
        "unblocked_by_admin_name": current_user.username,
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(unblocked_notice, session.user_id)
    await manager.broadcast_to_admins(unblocked_notice)

    return {
        "status": "success",
        "user_id": session.user_id,
    }


@router.post("/admin/reply")
async def admin_reply(
    request: AdminReplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    clean_message = request.message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(clean_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if (session.status or SESSION_STATUS_ACTIVE).upper() == SESSION_STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Chat is ended. Wait for user to reopen it.")
    if session.is_user_blocked:
        raise HTTPException(status_code=400, detail="User is blocked from chat")

    primary_session, all_sessions = await _get_or_create_user_support_session(db, session.user_id)
    user_session_ids = [s.id for s in all_sessions]

    new_msg = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=True,
    )
    db.add(new_msg)

    await _mark_user_attended(db, session.user_id, current_user.id)
    await _mark_user_messages_read(db, user_session_ids)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = _chat_message_event(
        message=new_msg,
        session_id=primary_session.id,
        user_id=session.user_id,
    )
    if not msg_data.get("timestamp"):
        msg_data["timestamp"] = now_ist().isoformat()

    await manager.send_personal_message(msg_data, session.user_id)
    await manager.broadcast_to_admins(msg_data)
    return {"status": "success"}


@router.post("/admin/upload")
async def admin_upload_media(
    session_id: int = Form(...),
    file: UploadFile = File(...),
    caption: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async),
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    clean_caption = (caption or "").strip()
    if len(clean_caption) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Caption is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if (session.status or SESSION_STATUS_ACTIVE).upper() == SESSION_STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Chat is ended. Wait for user to reopen it.")
    if session.is_user_blocked:
        raise HTTPException(status_code=400, detail="User is blocked from chat")

    try:
        stored_media = await store_support_media(
            upload_file=file,
            owner_user_id=session.user_id,
            sender_role="admin",
        )
    except SupportMediaValidationError as validation_error:
        raise HTTPException(status_code=400, detail=str(validation_error)) from validation_error
    except HTTPException:
        raise
    except Exception:
        logger.exception("Support media upload failed for admin_id=%s session_id=%s", current_user.id, session_id)
        raise HTTPException(status_code=500, detail="Unable to upload media right now")

    primary_session, all_sessions = await _get_or_create_user_support_session(db, session.user_id)
    user_session_ids = [s.id for s in all_sessions]

    new_msg = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=_caption_or_placeholder(clean_caption, stored_media.media_type),
        is_admin=True,
        media_type=stored_media.media_type,
        media_url=stored_media.public_url,
        media_path=stored_media.relative_path,
        media_mime_type=stored_media.mime_type,
        media_size_bytes=stored_media.size_bytes,
        media_expires_at=stored_media.expires_at,
    )
    db.add(new_msg)

    await _mark_user_attended(db, session.user_id, current_user.id)
    await _mark_user_messages_read(db, user_session_ids)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = _chat_message_event(
        message=new_msg,
        session_id=primary_session.id,
        user_id=session.user_id,
    )
    if not msg_data.get("timestamp"):
        msg_data["timestamp"] = now_ist().isoformat()

    await manager.send_personal_message(msg_data, session.user_id)
    await manager.broadcast_to_admins(msg_data)

    return {
        "status": "success",
        "message": _serialize_chat_message(new_msg),
    }
