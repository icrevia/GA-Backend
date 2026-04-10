from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
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


class AttendSessionRequest(BaseModel):
    session_id: int


class EndSessionRequest(BaseModel):
    session_id: int


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


async def _set_user_support_attendance(
    db: AsyncSession,
    user_id: int,
    admin_id: int | None,
    requires_admin: bool | None = None,
) -> None:
    updates_payload = {
        "attended_by_admin_id": admin_id,
        "attended_at": now_ist() if admin_id is not None else None,
    }
    if requires_admin is not None:
        updates_payload["requires_admin"] = requires_admin

    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id)
        .values(**updates_payload)
    )


async def _get_attending_admin_for_user(db: AsyncSession, user_id: int) -> User | None:
    attending_admin_result = await db.execute(
        select(ChatSession.attended_by_admin_id)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.attended_by_admin_id.isnot(None),
        )
        .order_by(ChatSession.attended_at.desc(), ChatSession.id.desc())
        .limit(1)
    )
    attending_admin_id = attending_admin_result.scalar_one_or_none()
    if not attending_admin_id:
        return None

    admin_user_result = await db.execute(select(User).where(User.id == int(attending_admin_id)))
    return admin_user_result.scalar_one_or_none()


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
            "is_admin":  m.is_admin,
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

    chat_user = aliased(User)
    attending_admin = aliased(User)

    rows_result = await db.execute(
        select(
            ChatSession.id.label("session_id"),
            ChatSession.user_id.label("user_id"),
            ChatSession.created_at.label("created_at"),
            ChatSession.requires_admin.label("requires_admin"),
            ChatSession.attended_by_admin_id.label("attended_by_admin_id"),
            ChatSession.attended_at.label("attended_at"),
            chat_user.username.label("username"),
            chat_user.email.label("email"),
            attending_admin.username.label("attended_by_admin_name"),
            latest_message_sq.c.content.label("last_message"),
            latest_message_sq.c.timestamp.label("last_timestamp"),
        )
        .join(chat_user, chat_user.id == ChatSession.user_id)
        .outerjoin(attending_admin, attending_admin.id == ChatSession.attended_by_admin_id)
        .outerjoin(
            latest_message_sq,
            and_(
                latest_message_sq.c.session_id == ChatSession.id,
                latest_message_sq.c.rn == 1,
            ),
        )
        .order_by(
            func.coalesce(ChatSession.requires_admin, False).desc(),
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
            "requires_admin": bool(row.requires_admin),
            "is_attended": bool(row.attended_by_admin_id),
            "attended_at": row.attended_at.isoformat() if row.attended_at else None,
            "attended_by_admin_id": row.attended_by_admin_id,
            "attended_by_admin_name": row.attended_by_admin_name,
            "unread": 0,
        }
        for row in rows
    ]


@router.get("/my-chat")
async def get_my_chat(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    session, all_sessions = await _get_or_create_user_support_session(db, current_user.id)
    attending_admin = await _get_attending_admin_for_user(db, current_user.id)
    session_ids = [s.id for s in all_sessions]

    messages_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id.in_(session_ids))
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
    )
    messages = messages_result.scalars().all()

    return {
        "session_id": session.id,
        "requires_admin": any(bool(s.requires_admin) for s in all_sessions),
        "is_attended": attending_admin is not None,
        "attended_by_admin_id": attending_admin.id if attending_admin else None,
        "attended_by_admin_name": attending_admin.username if attending_admin else None,
        "messages": [
            {
                "id":        m.id,
                "content":   m.content,
                "is_admin":  m.is_admin,
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
        is_admin=(current_user.role == "ADMIN")
    )
    db.add(new_msg)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "user_id":    session.user_id,
        "content":    new_msg.content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }
    await manager.broadcast(msg_data)

    if current_user.role == "ADMIN":
        return {"status": "success", "escalated_to_admin": False}

    attending_admin = await _get_attending_admin_for_user(db, session.user_id)
    await _set_user_support_attendance(
        db,
        user_id=session.user_id,
        admin_id=attending_admin.id if attending_admin else None,
        requires_admin=attending_admin is None,
    )
    await db.commit()

    if attending_admin is None:
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
        "escalated_to_admin": attending_admin is None,
        "attended_by_admin_id": attending_admin.id if attending_admin else None,
        "attended_by_admin_name": attending_admin.username if attending_admin else None,
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
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }

    if current_user.role == "ADMIN":
        await manager.send_personal_message(msg_data, target_user_id)
    else:
        await manager.broadcast_to_admins(msg_data)

    return {"status": "success"}


@router.post("/admin/attend")
async def admin_attend_session(
    request: AttendSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    existing_attender = await _get_attending_admin_for_user(db, session.user_id)
    if existing_attender and existing_attender.id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail=f"This chat is currently attended by Agent {existing_attender.username}.",
        )

    primary_session, _ = await _get_or_create_user_support_session(db, session.user_id)

    await _set_user_support_attendance(
        db,
        user_id=session.user_id,
        admin_id=current_user.id,
        requires_admin=False,
    )

    join_message = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=f"Agent {current_user.username} has joined this chat. Please talk to him.",
        is_admin=True,
    )
    db.add(join_message)
    await db.commit()
    await db.refresh(join_message)

    join_chat_payload = {
        "type": "chat_message",
        "id": join_message.id,
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "content": join_message.content,
        "is_admin": True,
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(join_chat_payload, session.user_id)
    await manager.broadcast_to_admins(join_chat_payload)

    attend_event = {
        "type": "support_attended",
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "attended_by_admin_id": current_user.id,
        "attended_by_admin_name": current_user.username,
        "timestamp": now_ist().isoformat(),
    }
    await manager.broadcast_to_admins(attend_event)

    return {
        "status": "success",
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "attended_by_admin_id": current_user.id,
        "attended_by_admin_name": current_user.username,
    }


@router.post("/admin/end")
async def admin_end_session(
    request: EndSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == request.session_id))
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    primary_session, _ = await _get_or_create_user_support_session(db, session.user_id)
    attending_admin = await _get_attending_admin_for_user(db, session.user_id)

    if attending_admin and attending_admin.id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail=f"This chat is currently attended by Agent {attending_admin.username}.",
        )

    if not attending_admin:
        return {
            "status": "success",
            "session_id": primary_session.id,
            "user_id": session.user_id,
            "ended_by_admin_id": None,
            "ended_by_admin_name": None,
            "already_ended": True,
        }

    await _set_user_support_attendance(
        db,
        user_id=session.user_id,
        admin_id=None,
        requires_admin=False,
    )

    leave_message = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=(
            f"Agent {current_user.username} has ended this live support session. "
            "Please send your message; next available agent will join shortly."
        ),
        is_admin=True,
    )
    db.add(leave_message)
    await db.commit()
    await db.refresh(leave_message)

    leave_chat_payload = {
        "type": "chat_message",
        "id": leave_message.id,
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "content": leave_message.content,
        "is_admin": True,
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(leave_chat_payload, session.user_id)
    await manager.broadcast_to_admins(leave_chat_payload)

    end_event = {
        "type": "support_unattended",
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "ended_by_admin_id": current_user.id,
        "ended_by_admin_name": current_user.username,
        "timestamp": now_ist().isoformat(),
    }
    await manager.broadcast_to_admins(end_event)

    return {
        "status": "success",
        "session_id": primary_session.id,
        "user_id": session.user_id,
        "ended_by_admin_id": current_user.id,
        "ended_by_admin_name": current_user.username,
        "already_ended": False,
    }


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
    attending_admin = await _get_attending_admin_for_user(db, session.user_id)
    if attending_admin and attending_admin.id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail=f"This chat is currently attended by Agent {attending_admin.username}.",
        )

    new_msg = ChatMessage(
        session_id=primary_session.id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=True
    )
    await _set_user_support_attendance(
        db,
        user_id=session.user_id,
        admin_id=current_user.id,
        requires_admin=False,
    )
    db.add(new_msg)
    await db.commit()
    await db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": primary_session.id,
        "user_id":    session.user_id,
        "content":    new_msg.content,
        "is_admin":   True,
        "timestamp":  now_ist().isoformat()
    }
    await manager.send_personal_message(msg_data, session.user_id)
    return {"status": "success"}
