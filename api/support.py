from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from sqlalchemy.orm import Session, aliased
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
import re

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

SUPPORT_BOT_GREETING_KEYWORDS = (
    "hi",
    "hii",
    "hello",
    "hey",
    "namaste",
    "namaskar",
)

SUPPORT_BOT_HELP_KEYWORDS = (
    "help",
    "madad",
    "support",
    "issue",
    "problem",
)

SUPPORT_BOT_MENU_RESPONSE = (
    "Hi! Main aapki help kar sakta hoon. Please issue type ke saath details bheje:\n"
    "1) Add money/payment not credited: amount + UTR\n"
    "2) Withdrawal pending: amount + request time\n"
    "3) Tournament join/match issue: tournament name + screenshot/error text\n"
    "4) Login/OTP issue: exact error message\n"
    "Direct human support ke liye type kare: human"
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


class AttendSessionRequest(BaseModel):
    session_id: int


class EndSessionRequest(BaseModel):
    session_id: int


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
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    words = set(normalized_text.split())

    for keyword in keywords:
        normalized_keyword = re.sub(r"[^a-z0-9]+", " ", keyword.lower()).strip()
        if not normalized_keyword:
            continue

        if " " in normalized_keyword:
            if normalized_keyword in normalized_text:
                return True
        elif normalized_keyword in words:
            return True

    return False


def _build_support_bot_reply(message: str) -> Tuple[str, bool, str]:
    normalized = " ".join(message.lower().split())
    words = [part for part in re.sub(r"[^a-z0-9]+", " ", normalized).split() if part]

    if _contains_any_keyword(normalized, SUPPORT_BOT_ESCALATION_KEYWORDS):
        return (
            "I am connecting you with a human support specialist right now. Please stay online.",
            True,
            "user_requested_human",
        )

    # Greeting and very short openers should get a guided menu, not escalation.
    if _contains_any_keyword(normalized, SUPPORT_BOT_GREETING_KEYWORDS) or (len(words) <= 2 and len(normalized) <= 12):
        return (SUPPORT_BOT_MENU_RESPONSE, False, "greeting_or_short_message")

    if _contains_any_keyword(normalized, SUPPORT_BOT_HELP_KEYWORDS):
        return (SUPPORT_BOT_MENU_RESPONSE, False, "help_menu")

    for intent_data in SUPPORT_BOT_INTENTS:
        if _contains_any_keyword(normalized, intent_data["keywords"]):
            return (intent_data["response"], False, intent_data["intent"])

    return (
        "I have alerted our human support team. Faster resolution ke liye please issue type, amount/UTR (if payment), and error/screenshot text share kare.",
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


def _get_or_create_user_support_session(db: Session, user_id: int) -> Tuple[ChatSession, List[ChatSession]]:
    """Returns a stable primary session plus all sessions for a user.

    Some users may have duplicate sessions created over time (network races/reinstalls).
    We keep writing to a deterministic primary session and read history from all sessions.
    """
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user_id)
        .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
        .all()
    )

    if sessions:
        return sessions[0], sessions

    session = ChatSession(user_id=user_id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, [session]


def _set_user_support_attendance(
    db: Session,
    user_id: int,
    admin_id: int | None,
    requires_admin: bool | None = None,
) -> None:
    updates: dict = {
        ChatSession.attended_by_admin_id: admin_id,
        ChatSession.attended_at: now_ist() if admin_id is not None else None,
    }
    if requires_admin is not None:
        updates[ChatSession.requires_admin] = requires_admin

    db.query(ChatSession).filter(ChatSession.user_id == user_id).update(
        updates,
        synchronize_session=False,
    )


def _get_attending_admin_for_user(db: Session, user_id: int) -> User | None:
    attending_admin_id = (
        db.query(ChatSession.attended_by_admin_id)
        .filter(
            ChatSession.user_id == user_id,
            ChatSession.attended_by_admin_id.isnot(None),
        )
        .order_by(ChatSession.attended_at.desc(), ChatSession.id.desc())
        .first()
    )
    if not attending_admin_id or not attending_admin_id[0]:
        return None

    return db.query(User).filter(User.id == int(attending_admin_id[0])).first()


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

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    user_session_ids = [
        sid for (sid,) in db.query(ChatSession.id).filter(ChatSession.user_id == session.user_id).all()
    ]
    if not user_session_ids:
        user_session_ids = [session_id]

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id.in_(user_session_ids)
    ).order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc()).all()

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

    chat_user = aliased(User)
    attending_admin = aliased(User)

    rows = (
        db.query(
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
            "is_attended": bool(row.attended_by_admin_id),
            "attended_at": row.attended_at.isoformat() if row.attended_at else None,
            "attended_by_admin_id": row.attended_by_admin_id,
            "attended_by_admin_name": row.attended_by_admin_name,
            "unread": 0,
        }
        for row in rows
    ]


@router.get("/my-chat")
def get_my_chat(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_user_for_support)
):
    session, all_sessions = _get_or_create_user_support_session(db, current_user.id)
    attending_admin = _get_attending_admin_for_user(db, current_user.id)
    session_ids = [s.id for s in all_sessions]

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id.in_(session_ids)
    ).order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc()).all()

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

    session, _ = _get_or_create_user_support_session(db, current_user.id)

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
        "user_id":    session.user_id,
        "content":    new_msg.content,
        "is_admin":   new_msg.is_admin,
        "timestamp":  now_ist().isoformat()
    }
    await manager.broadcast(msg_data)

    if current_user.role == "ADMIN":
        return {"status": "success", "escalated_to_admin": False}

    # When a human agent is actively attending this user, pause bot auto-replies.
    attending_admin = _get_attending_admin_for_user(db, session.user_id)
    if attending_admin:
        _set_user_support_attendance(
            db,
            user_id=session.user_id,
            admin_id=attending_admin.id,
            requires_admin=False,
        )
        return {
            "status": "success",
            "escalated_to_admin": False,
            "bot_reason": "human_attending",
            "attended_by_admin_id": attending_admin.id,
            "attended_by_admin_name": attending_admin.username,
        }

    bot_reply, should_escalate, escalation_reason = _build_support_bot_reply(clean_message)
    bot_sender_id = _resolve_bot_sender_id(db, current_user.id)

    bot_msg = ChatMessage(
        session_id=session.id,
        sender_id=bot_sender_id,
        content=bot_reply,
        is_admin=True,
    )

    if should_escalate:
        _set_user_support_attendance(
            db,
            user_id=session.user_id,
            admin_id=None,
            requires_admin=True,
        )

    db.add(bot_msg)
    db.commit()
    db.refresh(bot_msg)

    bot_data = {
        "type": "chat_message",
        "id": bot_msg.id,
        "session_id": session.id,
        "user_id": session.user_id,
        "content": bot_msg.content,
        "is_admin": True,
        "timestamp": now_ist().isoformat(),
    }
    user_delivery_ok = await manager.send_personal_message(bot_data, session.user_id)
    await manager.broadcast_to_admins(bot_data)
    logger.info(
        "Support bot reply generated: session_id=%s user_id=%s reason=%s escalated=%s delivered_to_user_ws=%s",
        session.id,
        session.user_id,
        escalation_reason,
        should_escalate,
        user_delivery_ok,
    )

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
    session, _ = _get_or_create_user_support_session(db, target_user_id)

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session = db.query(ChatSession).filter(ChatSession.id == request.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    existing_attender = _get_attending_admin_for_user(db, session.user_id)
    if existing_attender and existing_attender.id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail=f"This chat is currently attended by Agent {existing_attender.username}.",
        )

    primary_session, _ = _get_or_create_user_support_session(db, session.user_id)

    _set_user_support_attendance(
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
    db.commit()
    db.refresh(join_message)

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")

    session = db.query(ChatSession).filter(ChatSession.id == request.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    primary_session, _ = _get_or_create_user_support_session(db, session.user_id)
    attending_admin = _get_attending_admin_for_user(db, session.user_id)

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

    _set_user_support_attendance(
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
            "AI assistant is active again. Human support ke liye 'human' type kare."
        ),
        is_admin=True,
    )
    db.add(leave_message)
    db.commit()
    db.refresh(leave_message)

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

    primary_session, _ = _get_or_create_user_support_session(db, session.user_id)
    attending_admin = _get_attending_admin_for_user(db, session.user_id)
    if not attending_admin:
        raise HTTPException(status_code=409, detail="Click Attend Now before replying to this chat.")
    if attending_admin.id != current_user.id:
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
    _set_user_support_attendance(
        db,
        user_id=session.user_id,
        admin_id=current_user.id,
        requires_admin=False,
    )
    db.add(new_msg)
    db.commit()

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
