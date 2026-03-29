from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from pydantic import BaseModel, Field
from typing import List, Tuple
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

SUPPORT_BOT_ESCALATION_KEYWORDS = (
    "human",
    "agent",
    "admin",
    "representative",
    "not solved",
    "not helpful",
    "didnt help",
    "didn't help",
    "escalate",
    "complaint",
)

SUPPORT_BOT_INTENTS = (
    {
        "intent": "wallet_add",
        "keywords": ("add money", "deposit", "payment", "upi", "utr", "transaction", "credited"),
        "response": (
            "I can help with add-money issues. Please share the amount, transaction time, and UTR/transaction ID. "
            "If money is deducted but not credited, I will keep this chat ready for priority admin review."
        ),
    },
    {
        "intent": "withdrawal",
        "keywords": ("withdraw", "withdrawal", "cashout", "payout"),
        "response": (
            "For withdrawal checks, please share amount and request time. "
            "If it is pending longer than usual, an admin will review this session first."
        ),
    },
    {
        "intent": "tournament",
        "keywords": ("tournament", "join", "match", "entry fee", "room id"),
        "response": (
            "For tournament issues, please share tournament name and screenshot/error text. "
            "Quick checks: stable internet, latest app version, and sufficient wallet balance."
        ),
    },
    {
        "intent": "login",
        "keywords": ("login", "otp", "password", "signin", "sign in", "account locked"),
        "response": (
            "For login problems, please retry OTP after 60 seconds and confirm network is stable. "
            "If still blocked, share your registered email/phone format (masked) and exact error message."
        ),
    },
    {
        "intent": "referral",
        "keywords": ("referral", "invite", "code", "bonus"),
        "response": (
            "Referral rewards are added after eligible completion criteria. "
            "Please share referral code and referred username so support can verify quickly."
        ),
    },
)


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


def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _build_support_bot_reply(message: str) -> Tuple[str, bool, str]:
    normalized = " ".join(message.lower().split())

    if _contains_any_keyword(normalized, SUPPORT_BOT_ESCALATION_KEYWORDS):
        return (
            "I am connecting you with a human support specialist right now. Please stay online.",
            True,
            "user_requested_human",
        )

    for intent_data in SUPPORT_BOT_INTENTS:
        if _contains_any_keyword(normalized, intent_data["keywords"]):
            return (intent_data["response"], False, intent_data["intent"])

    return (
        "I could not confidently understand this issue. I have alerted our human support team, and they will reply shortly.",
        True,
        "bot_not_confident",
    )


def _resolve_bot_sender_id(db: Session, fallback_user_id: int) -> int:
    admin_user = (
        db.query(User.id)
        .filter(User.role == "ADMIN", User.is_active.is_(True))
        .order_by(User.id.asc())
        .first()
    )
    if admin_user and admin_user[0]:
        return int(admin_user[0])
    return fallback_user_id


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
            ChatSession.requires_admin.label("requires_admin"),
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
            func.coalesce(ChatSession.requires_admin, False).desc(),
            func.coalesce(latest_message_sq.c.timestamp, ChatSession.created_at).desc(),
        )
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
            "requires_admin": bool(row.requires_admin),
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
        "requires_admin": bool(session.requires_admin),
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
    db.refresh(new_msg)

    msg_data = {
        "type":       "chat_message",
        "id":         new_msg.id,
        "session_id": session.id,
        "content":    new_msg.content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }
    await manager.broadcast(msg_data)

    if current_user.role == "ADMIN":
        return {"status": "success", "escalated_to_admin": False}

    bot_reply, should_escalate, escalation_reason = _build_support_bot_reply(clean_message)
    bot_sender_id = _resolve_bot_sender_id(db, current_user.id)

    bot_msg = ChatMessage(
        session_id=session.id,
        sender_id=bot_sender_id,
        content=bot_reply,
        is_admin=True,
    )

    if should_escalate:
        session.requires_admin = True

    db.add(bot_msg)
    db.commit()
    db.refresh(bot_msg)

    bot_data = {
        "type": "chat_message",
        "id": bot_msg.id,
        "session_id": session.id,
        "content": bot_msg.content,
        "is_admin": True,
        "timestamp": now_ist().isoformat(),
    }
    await manager.send_personal_message(bot_data, session.user_id)
    await manager.broadcast_to_admins(bot_data)

    if should_escalate:
        escalation_event = {
            "type": "support_escalation",
            "session_id": session.id,
            "user_id": session.user_id,
            "from_user_id": session.user_id,
            "from_user_name": current_user.username,
            "message_preview": clean_message[:180],
            "reason": escalation_reason,
            "timestamp": now_ist().isoformat(),
        }
        await manager.broadcast_to_admins(escalation_event)

    return {
        "status": "success",
        "escalated_to_admin": should_escalate,
        "bot_reason": escalation_reason,
    }


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
    session.requires_admin = False
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
