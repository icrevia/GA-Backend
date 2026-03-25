from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import List
from core.database import get_db
from api.deps import get_current_user
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata

def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)  # Store as naive IST in DB
from pydantic import BaseModel

router = APIRouter()

# --- Request Models ---
class AdminReplyRequest(BaseModel):
    session_id: int
    message: str

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)

@router.get("/sessions/{session_id}/messages", response_model=List[dict])
def get_session_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.timestamp.asc()).all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "is_admin": m.is_admin,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None
        } for m in messages
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
        last_msg = db.query(ChatMessage).filter(ChatMessage.session_id == s.id).order_by(ChatMessage.timestamp.desc()).first()
        result.append({
            "id": s.id,
            "user_id": s.user_id,
            "user": {
                "username": s.user.username,
                "email": s.user.email
            },
            "last_message": last_msg.content if last_msg else "No messages yet",
            "last_timestamp": (last_msg.timestamp if last_msg else s.created_at).isoformat() if (last_msg or s.created_at) else None,
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
    
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.timestamp.asc()).all()
    
    return {
        "session_id": session.id,
        "messages": [
            {
                "id": m.id,
                "content": m.content,
                "is_admin": m.is_admin,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None
            } for m in messages
        ]
    }

@router.post("/send")
async def send_message(
    message: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    session = db.query(ChatSession).filter(ChatSession.user_id == current_user.id).first()
    if not session:
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)
    
    new_msg = ChatMessage(
        session_id=session.id,
        sender_id=current_user.id,
        content=message,
        is_admin=(current_user.role == "ADMIN")
    )
    db.add(new_msg)
    db.commit()
    
    msg_data = {
        "id": new_msg.id,
        "content": new_msg.content,
        "is_admin": new_msg.is_admin,
        "timestamp": now_ist().isoformat()
    }
    await manager.broadcast(msg_data)
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
        "id": new_msg.id,
        "content": new_msg.content,
        "is_admin": True,
        "timestamp": now_ist().isoformat()
    }
    await manager.broadcast(msg_data)
    return {"status": "success"}
