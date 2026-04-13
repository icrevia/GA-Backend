from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import aliased
from pydantic import BaseModel, Field
from typing import List, Optional
from core.database import get_db
from api.deps import get_current_user_async, get_user_for_support_async
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from core.config import settings
from services.support_media import SupportMediaValidationError, store_support_media
from services.support_notifications import notify_support_message, notify_admin_escalation
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
    if not raw_value: return None
    clean = raw_value.strip()
    return clean[:MAX_ISSUE_TYPE_LENGTH] if clean else None

def _support_whatsapp_url() -> str:
    digits = "".join(ch for ch in (settings.SUPPORT_WHATSAPP_NUMBER or "") if ch.isdigit())
    if not digits: digits = "917632932544"
    return f"https://wa.me/{digits}"

router = APIRouter()

# ─────────────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)
    issue_type: Optional[str] = Field(default=None, max_length=MAX_ISSUE_TYPE_LENGTH)
    is_issue_selection: bool = False

class AdminReplyRequest(BaseModel):
    user_id: int # Target user
    message: str = Field(min_length=1, max_length=MAX_SUPPORT_MESSAGE_LENGTH)

class AdminStatusRequest(BaseModel):
    user_id: int

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

async def _get_or_init_support_metadata(db: AsyncSession, user_id: int) -> ChatSession:
    """Gets the single persistent metadata record for a user's support thread."""
    # Handle legacy duplicates by picking the most recent session record
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.id.desc())
        .limit(1)
    )
    session = result.scalars().first()
    if not session:
        session = ChatSession(user_id=user_id, status=SESSION_STATUS_ACTIVE)
        db.add(session)
        await db.commit()
        await db.refresh(session)
    return session

async def _mark_messages_read(db: AsyncSession, thread_user_id: int, by_admin: bool) -> None:
    """WhatsApp-style read receipts."""
    now = now_ist()
    query = update(ChatMessage).where(
        ChatMessage.thread_user_id == thread_user_id,
        ChatMessage.is_read == False
    )
    if by_admin:
        # Admin is reading messages from User
        query = query.where(ChatMessage.is_admin == False)
    else:
        # User is reading messages from Admin
        query = query.where(ChatMessage.is_admin == True)

    await db.execute(query.values(is_read=True, read_at=now, is_delivered=True, delivered_at=now))

def _serialize_msg(msg: ChatMessage) -> dict:
    return {
        "id": msg.id,
        "content": msg.content,
        "is_admin": bool(msg.is_admin),
        "is_read": bool(msg.is_read),
        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        "media_type": msg.media_type,
        "media_url": msg.media_url,
    }

def _msg_event(msg: ChatMessage, thread_user_id: int) -> dict:
    return {
        "type": "chat_message",
        "user_id": thread_user_id,
        **_serialize_msg(msg)
    }

# ─────────────────────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────────────────────

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, token: str = "", db: AsyncSession = Depends(get_db)):
    from jose import jwt, JWTError
    if not token or token in ("null", "undefined"):
        await websocket.close(code=1008)
        return
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        token_uid = int(payload.get("sub", -1))
    except (JWTError, ValueError):
        await websocket.close(code=1008)
        return

    # Verify admin role
    user_result = await db.execute(select(User.role).where(User.id == token_uid))
    role = user_result.scalar_one_or_none() or "USER"
    is_admin = (role == "ADMIN")

    await websocket.accept()
    await manager.connect(user_id, websocket, is_admin=is_admin)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)

# ─────────────────────────────────────────────────────────────────
# User Endpoints
# ─────────────────────────────────────────────────────────────────

@router.get("/my-chat")
async def get_my_chat(
    since_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    """Fetch persistent thread history."""
    meta = await _get_init_support_metadata(db, current_user.id)
    
    query = select(ChatMessage).where(ChatMessage.thread_user_id == current_user.id)
    if since_id: query = query.where(ChatMessage.id > since_id)
    query = query.order_by(ChatMessage.timestamp.desc(), ChatMessage.id.desc()).limit(100)
    
    res = await db.execute(query)
    messages = list(reversed(res.scalars().all()))
    
    # Mark as read
    await _mark_messages_read(db, current_user.id, by_admin=False)
    await db.commit()

    return {
        "is_user_blocked": bool(meta.is_user_blocked),
        "blocked_message": meta.blocked_message or DEFAULT_BLOCKED_MESSAGE if meta.is_user_blocked else None,
        "support_whatsapp_url": _support_whatsapp_url(),
        "attended_by_admin_id": meta.attended_by_admin_id,
        "messages": [_serialize_msg(m) for m in messages]
    }

@router.post("/send")
async def user_send(
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    meta = await _get_init_support_metadata(db, current_user.id)
    if meta.is_user_blocked:
        raise HTTPException(status_code=403, detail=meta.blocked_message or DEFAULT_BLOCKED_MESSAGE)

    new_msg = ChatMessage(
        thread_user_id=current_user.id,
        sender_id=current_user.id,
        content=req.message,
        is_admin=False
    )
    db.add(new_msg)
    
    meta.requires_admin = True
    meta.status = SESSION_STATUS_ACTIVE
    if req.issue_type: meta.issue_type = _normalize_issue_type(req.issue_type)
    
    await db.commit()
    await db.refresh(new_msg)
    
    event = _msg_event(new_msg, current_user.id)
    await notify_support_message(db, current_user.id, event, is_to_admin=True)
    if req.is_issue_selection or not meta.attended_by_admin_id:
        await notify_admin_escalation(db, current_user, event)
    
    return {"status": "success", "message": _serialize_msg(new_msg)}

@router.post("/upload")
async def user_upload(
    caption: str = Form(default=""),
    issue_type: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_for_support_async)
):
    meta = await _get_init_support_metadata(db, current_user.id)
    if meta.is_user_blocked:
        raise HTTPException(status_code=403, detail=DEFAULT_BLOCKED_MESSAGE)
    
    try:
        media = await store_support_media(file)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_msg = ChatMessage(
        thread_user_id=current_user.id,
        sender_id=current_user.id,
        content=caption if caption else (media["media_type"].capitalize()),
        is_admin=False,
        media_type=media["media_type"],
        media_url=media["media_url"]
    )
    db.add(new_msg)
    meta.requires_admin = True
    await db.commit()
    await db.refresh(new_msg)
    
    event = _msg_event(new_msg, current_user.id)
    await notify_support_message(db, current_user.id, event, is_to_admin=True)
    return {"status": "success", "message": _serialize_msg(new_msg)}

# ─────────────────────────────────────────────────────────────────
# Admin Endpoints
# ─────────────────────────────────────────────────────────────────

@router.get("/admin/threads")
async def get_admin_threads(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    
    # Subquery for latest session per user to avoid duplications
    latest_session_sq = select(
        ChatSession.user_id,
        func.max(ChatSession.id).label("max_id")
    ).group_by(ChatSession.user_id).subquery()

    # Subquery for latest message
    latest_sq = select(
        ChatMessage.thread_user_id,
        ChatMessage.content,
        ChatMessage.timestamp
    ).distinct(ChatMessage.thread_user_id).order_by(
        ChatMessage.thread_user_id, ChatMessage.timestamp.desc()
    ).subquery()

    # Subquery for unread
    unread_sq = select(
        ChatMessage.thread_user_id,
        func.count(ChatMessage.id).label("count")
    ).where(ChatMessage.is_admin == False, ChatMessage.is_read == False).group_by(ChatMessage.thread_user_id).subquery()

    query = select(
        User.id.label("user_id"),
        User.username,
        ChatSession.requires_admin,
        ChatSession.is_user_blocked,
        latest_sq.c.content.label("last_msg"),
        latest_sq.c.timestamp.label("last_time"),
        func.coalesce(unread_sq.c.count, 0).label("unread")
    ).join(latest_session_sq, latest_session_sq.c.user_id == User.id)\
     .join(ChatSession, ChatSession.id == latest_session_sq.c.max_id)\
     .outerjoin(latest_sq, latest_sq.c.thread_user_id == User.id)\
     .outerjoin(unread_sq, unread_sq.c.thread_user_id == User.id)\
     .order_by(ChatSession.requires_admin.desc(), latest_sq.c.timestamp.desc())

    res = await db.execute(query)
    return [dict(r._asdict()) for r in res.all()]

@router.get("/admin/thread/{user_id}")
async def get_admin_thread_history(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    
    res = await db.execute(
        select(ChatMessage).where(ChatMessage.thread_user_id == user_id).order_by(ChatMessage.timestamp.asc())
    )
    messages = res.scalars().all()
    
    await _mark_messages_read(db, user_id, by_admin=True)
    await db.commit()
    return [_serialize_msg(m) for m in messages]

@router.post("/admin/reply")
async def admin_reply(
    req: AdminReplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_async)
):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    
    meta = await _get_init_support_metadata(db, req.user_id)
    new_msg = ChatMessage(
        thread_user_id=req.user_id,
        sender_id=current_user.id,
        content=req.message,
        is_admin=True
    )
    db.add(new_msg)
    meta.requires_admin = False
    meta.attended_by_admin_id = current_user.id
    
    await db.commit()
    await db.refresh(new_msg)
    
    event = _msg_event(new_msg, req.user_id)
    await notify_support_message(db, req.user_id, event, is_to_admin=False)
    return {"status": "success", "message": _serialize_msg(new_msg)}

@router.post("/admin/attend")
async def admin_attend(req: AdminStatusRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_async)):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    meta = await _get_init_support_metadata(db, req.user_id)
    meta.attended_by_admin_id = current_user.id
    meta.requires_admin = False
    await db.commit()
    return {"status": "attended"}

@router.post("/admin/end")
async def admin_end(req: AdminStatusRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_async)):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    meta = await _get_init_support_metadata(db, req.user_id)
    meta.requires_admin = False
    meta.issue_type = None
    # Optionally we could send a message: "Chat marked as resolved by admin."
    await db.commit()
    return {"status": "resolved"}

@router.post("/admin/block")
async def admin_block(req: AdminStatusRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_async)):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    meta = await _get_init_support_metadata(db, req.user_id)
    meta.is_user_blocked = True
    await db.commit()
    return {"status": "blocked"}

@router.post("/admin/unblock")
async def admin_unblock(req: AdminStatusRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_async)):
    if current_user.role != "ADMIN": raise HTTPException(status_code=403)
    meta = await _get_init_support_metadata(db, req.user_id)
    meta.is_user_blocked = False
    await db.commit()
    return {"status": "unblocked"}

# Alias for backward compatibility during migration
_get_init_support_metadata = _get_or_init_support_metadata
