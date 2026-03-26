from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List
from core.database import get_db
from api.deps import get_current_user
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("zexplay.support")
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────

class AdminReplyRequest(BaseModel):
    session_id: int
    message: str


class SendMessageRequest(BaseModel):
    message: str   # FIXED: body instead of query param (no longer logged in access logs)


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

    sessions = db.query(ChatSession).join(User).all()
    result = []
    for s in sessions:
        last_msg = db.query(ChatMessage).filter(
            ChatMessage.session_id == s.id
        ).order_by(ChatMessage.timestamp.desc()).first()

        result.append({
            "id":             s.id,
            "user_id":        s.user_id,
            "user": {
                "username": s.user.username,
                "email":    s.user.email
            },
            "last_message":   last_msg.content if last_msg else "No messages yet",
            "last_timestamp": (
                (last_msg.timestamp if last_msg else s.created_at).isoformat()
                if (last_msg or s.created_at) else None
            ),
            "unread": 0
        })
    return result


@router.get("/my-chat")
def get_my_chat(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    session = db.query(ChatSession).filter(ChatSession.user_id == current_user.id).first()
    if not session:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=body.message.strip(),
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
    current_user: User = Depends(get_current_user)
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

    session = db.query(ChatSession).filter(ChatSession.id == request.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    new_msg = ChatMessage(
        session_id=request.session_id,
        sender_id=current_user.id,
        content=request.message,
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
