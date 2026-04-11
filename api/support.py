from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, select, update
from pydantic import BaseModel, Field
from typing import List, Tuple
from core.database import get_db
from api.deps import get_current_user_async, get_user_for_support_async
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("GamerzAdda.support")
IST = timezone(timedelta(hours=5, minutes=30))
MAX_SUPPORT_MESSAGE_LENGTH = 1000


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────

class AdminReplyRequest(BaseModel):
    session_id: int
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)


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
        return sessions[0], sessions

    session = ChatSession(user_id=user_id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session, [session]


async def _clear_user_support_flags(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(
            requires_admin=False,
            attended_by_admin_id=None,
            attended_at=None,
        )
    )


# ─────────────────────────────────────────────────────────────────
# WebSocket — FIXED: requires JWT token, verifies ownership
# ─────────────────────────────────────────────────────────────────

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, token: str = ""):
    """Support WebSocket — authenticated, user can only connect as themselves."""
    import json
    from jose import jwt, JWTError
    from core.config import settings
    from core.database import SessionLocal

    # FIXED: Validate the JWT token before accepting the connection
    if not token or token in ("null", "undefined", ""):
        await websocket.close(code=1008)
        return

    try:
        payload    = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        token_uid  = int(payload.get("sub", -1))
    except (JWTError, ValueError):
        await websocket.close(code=1008)
        return

    # FIXED: Enforce ownership — a user cannot spoof another user's connection
    if token_uid != user_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await manager.connect(user_id, websocket)
    logger.info(f"Support WS connected: user_id={user_id}")
    try:
        while True:
            await websocket.receive_text()  # Keep-alive receive; actual messages go via REST
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        logger.info(f"Support WS disconnected: user_id={user_id}")


# ─────────────────────────────────────────────────────────────────
# Session & message endpoints
# ─────────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/messages", response_model=List[dict])
async def get_session_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
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

    return [
        {
            "id":        m.id,
            "content":   m.content,
            "is_admin":  (m.sender_id != session.user_id),
            "timestamp": m.timestamp.isoformat() if m.timestamp else None
        }
        for m in messages
    ]


@router.get("/sessions", response_model=List[dict])
async def get_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    latest_message_sq = (
        select(
            ChatMessage.session_id.label("session_id"),
            ChatMessage.content.label("content"),
            ChatMessage.timestamp.label("timestamp"),
            func.row_number().over(
                partition_by=ChatMessage.session_id,
                order_by=(ChatMessage.timestamp.desc(), ChatMessage.id.desc())
            ).label("rn"),
        )
        .subquery()
    )

    rows_result = await db.execute(
        select(
            ChatSession.id.label("session_id"),
            ChatSession.user_id.label("user_id"),
            ChatSession.created_at.label("created_at"),
            User.username.label("username"),
            User.email.label("email"),
            latest_message_sq.c.content.label("last_message"),
            latest_message_sq.c.timestamp.label("last_timestamp"),
        )
        .join(User, User.id == ChatSession.user_id)
        .outerjoin(
            latest_message_sq,
            and_(
                latest_message_sq.c.session_id == ChatSession.id,
                latest_message_sq.c.rn == 1,
            ),
        )
        .order_by(
            func.coalesce(latest_message_sq.c.timestamp, ChatSession.created_at).desc(),
        )
    )
    rows = rows_result.all()

    return [
        {
            "id": row.session_id,
            "user_id": row.user_id,
            "user": {
                "username": row.username,
                "email": row.email,
            },
            "last_message": row.last_message or "No messages yet",
            "last_timestamp": (
                (row.last_timestamp or row.created_at).isoformat()
                if (row.last_timestamp or row.created_at) else None
            ),
            "requires_admin": False,
            "is_attended": False,
            "attended_at": None,
            "attended_by_admin_id": None,
            "attended_by_admin_name": None,
            "unread": 0,
        }
        for row in rows
    ]


@router.get("/my-chat")
async def get_my_chat(
    since_id: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    session_ids = [s.id for s in all_sessions]

    message_query = (
        select(ChatMessage)
        .where(ChatMessage.session_id.in_(session_ids))
    )
    if since_id > 0:
        message_query = message_query.where(ChatMessage.id > since_id)

    messages_result = await db.execute(
        message_query.order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
    )
    messages = messages_result.scalars().all()

    return {
        "session_id": session.id,
        "requires_admin": False,
        "is_attended": False,
        "attended_by_admin_id": None,
        "attended_by_admin_name": None,
        "messages": [
            {
                "id":        m.id,
                "content":   m.content,
                "is_admin":  (m.sender_id != session.user_id),
                "timestamp": m.timestamp.isoformat() if m.timestamp else None
            }
            for m in messages
        ]
    }


# FIXED: Message sent as JSON body, not query parameter (no longer written to server access logs)
@router.post("/send")
async def send_message(
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    clean_message = body.message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(clean_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session, _ = await _get_or_create_user_support_session(db, current_user.id)

    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=clean_message,
        # /support/send is a user-facing endpoint; always persist as user message
        is_admin=False
    )
    db.add(new_msg)
    await _clear_user_support_flags(db, session.user_id)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "user_id":    session.user_id,
        "content":    new_msg.content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  new_msg.timestamp.isoformat() if new_msg.timestamp else now_ist().isoformat()
    }
    await manager.broadcast(msg_data)

    escalation_event = {
        "type": "support_escalation",
        "session_id": session.id,
        "user_id": session.user_id,
        "from_user_id": session.user_id,
        "from_user_name": current_user.username,
        "message_preview": clean_message[:180],
        "reason": "user_message",
        "timestamp": now_ist().isoformat(),
    }
    await manager.broadcast_to_admins(escalation_event)

    return {
        "status": "success",
        "escalated_to_admin": True,
        "attended_by_admin_id": None,
        "attended_by_admin_name": None,
    }


@router.post("/log-call")
async def log_call(
    type: str = Query(...),            # MISSED, REJECTED, FINISHED, BUSY
    duration: str = Query(None),
    user_id: int = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    target_user_id = user_id if (current_user.role == "ADMIN" and user_id) else current_user.id
    session, _ = await _get_or_create_user_support_session(db, target_user_id)

    content = f"[CALL_LOG:{type}]"
    if duration:
        content = f"[CALL_LOG:{type}:{duration}]"

    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=content,
        is_admin=(current_user.role == "ADMIN")
    )
    db.add(new_msg)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "user_id":    target_user_id,
        "content":    content,
        "is_admin":   (new_msg.sender_id != target_user_id),
        "timestamp":  new_msg.timestamp.isoformat() if new_msg.timestamp else now_ist().isoformat()
    }

    if current_user.role == "ADMIN":
        await manager.send_personal_message(msg_data, target_user_id)
    else:
        await manager.broadcast_to_admins(msg_data)

    return {"status": "success"}


@router.post("/admin/reply")
async def admin_reply(
    request: AdminReplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
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

    primary_session, _ = await _get_or_create_user_support_session(db, session.user_id)

    new_msg = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=True
    )
    db.add(new_msg)
    await _clear_user_support_flags(db, session.user_id)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": primary_session.id,
        "user_id":    session.user_id,
        "content":    new_msg.content,
        "is_admin":   True,
        "timestamp":  new_msg.timestamp.isoformat() if new_msg.timestamp else now_ist().isoformat()
    }
    await manager.send_personal_message(msg_data, session.user_id)
    await manager.broadcast_to_admins(msg_data)
    return {"status": "success"}
