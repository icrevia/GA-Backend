from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from pydantic import BaseModel, Field
from typing import List
from core.database import get_db
from api.deps import get_current_user, get_user_for_support
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from datetime import datetime, timezone, timedelta
from collections import deque
from threading import Lock
import logging

logger = logging.getLogger("zexplay.support")
IST = timezone(timedelta(hours=5, minutes=30))
MAX_SUPPORT_MESSAGE_LENGTH = 1000
SUPPORT_RATE_LIMIT_WINDOW_SECONDS = 60
SUPPORT_RATE_LIMIT_PER_MIN_USER = 20
SUPPORT_RATE_LIMIT_PER_MIN_ADMIN = 60
SUPPORT_RATE_LIMIT_PER_MIN_IP = 120

_SUPPORT_IP_BUCKETS: dict[str, deque[datetime]] = {}
_SUPPORT_IP_BUCKETS_LOCK = Lock()


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


def _extract_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else "unknown"


def _check_ip_rate_limit(client_ip: str) -> bool:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=SUPPORT_RATE_LIMIT_WINDOW_SECONDS)

    with _SUPPORT_IP_BUCKETS_LOCK:
        bucket = _SUPPORT_IP_BUCKETS.setdefault(client_ip, deque())

        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= SUPPORT_RATE_LIMIT_PER_MIN_IP:
            return False

        bucket.append(now)

    return True


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
def get_session_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.timestamp.asc()).all()

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
def get_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    latest_message_sq = (
        db.query(
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

    rows = (
        db.query(
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
        .order_by(func.coalesce(latest_message_sq.c.timestamp, ChatSession.created_at).desc())
        .all()
    )

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
            "unread": 0,
        }
        for row in rows
    ]


@router.get("/my-chat")
def get_my_chat(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_user_for_support)
):
    session = db.query(ChatSession).filter(ChatSession.user_id == current_user.id).first()
    if not session:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id == session.id
    ).order_by(ChatMessage.timestamp.asc()).all()

    return {
        "session_id": session.id,
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_user_for_support)
):
    clean_message = body.message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(clean_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    window_start = datetime.now(timezone.utc) - timedelta(seconds=SUPPORT_RATE_LIMIT_WINDOW_SECONDS)
    max_allowed = SUPPORT_RATE_LIMIT_PER_MIN_ADMIN if current_user.role == "ADMIN" else SUPPORT_RATE_LIMIT_PER_MIN_USER
    recent_count = db.query(ChatMessage).filter(
        ChatMessage.sender_id == current_user.id,
        ChatMessage.timestamp >= window_start,
    ).count()
    if recent_count >= max_allowed:
        raise HTTPException(status_code=429, detail="Too many messages. Please slow down.")

    client_ip = _extract_client_ip(request)
    if not _check_ip_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests from this network. Please slow down.")

    session = db.query(ChatSession).filter(ChatSession.user_id == current_user.id).first()
    if not session:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=(current_user.role == "ADMIN")
    )
    db.add(new_msg)
    db.commit()

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "content":    new_msg.content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }
    await manager.broadcast(msg_data)
    return {"status": "success"}


@router.post("/log-call")
async def log_call(
    type: str = Query(...),            # MISSED, REJECTED, FINISHED, BUSY
    duration: str = Query(None),
    user_id: int = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_user_for_support)
):
    target_user_id = user_id if (current_user.role == "ADMIN" and user_id) else current_user.id
    session = db.query(ChatSession).filter(ChatSession.user_id == target_user_id).first()
    if not session:
        session = ChatSession(user_id=target_user_id)
        db.add(session)
        db.commit()
        db.refresh(session)

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
    db.commit()

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "content":    content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }

    if current_user.role == "ADMIN":
        await manager.send_personal_message(msg_data, target_user_id)
    else:
        await manager.broadcast_to_admins(msg_data)

    return {"status": "success"}


@router.post("/admin/reply")
async def admin_reply(
    request: AdminReplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    clean_message = request.message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(clean_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message is too long (max {MAX_SUPPORT_MESSAGE_LENGTH} characters)")

    session = db.query(ChatSession).filter(ChatSession.id == request.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    new_msg = ChatMessage(
        session_id=request.session_id,
        sender_id=current_user.id,
        content=clean_message,
        is_admin=True
    )
    db.add(new_msg)
    db.commit()

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": request.session_id,
        "content":    new_msg.content,
        "is_admin":   True,
        "timestamp":  now_ist().isoformat()
    }
    await manager.send_personal_message(msg_data, session.user_id)
    return {"status": "success"}
