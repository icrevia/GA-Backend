from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List
from core.database import get_db
from api.deps import get_current_user
from models.support import ChatSession, ChatMessage
from models.user import User

router = APIRouter()

@router.get("/sessions", response_model=List[dict])
def get_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Admin only: get all active chat sessions with user metadata"""
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    sessions = db.query(ChatSession).all()
    result = []
    for s in sessions:
        last_msg = db.query(ChatMessage).filter(ChatMessage.session_id == s.id).order_by(ChatMessage.timestamp.desc()).first()
        result.append({
            "id": s.id,
            "user_id": s.user_id,
            "status": s.status,
            "created_at": s.created_at,
            "user": {
                "username": s.user.username,
                "email": s.user.email
            },
            "last_message": last_msg.content if last_msg else "No messages yet",
            "last_timestamp": last_msg.timestamp if last_msg else s.created_at,
            "unread": 0 # Logic to be added
        })
    return result

@router.get("/my-chat", response_model=List[dict])
def get_my_chat(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """User/Admin: get current session messages"""
    session = db.query(ChatSession).filter(ChatSession.user_id == current_user.id).first()
    if not session:
        # Create a session if it doesn't exist
        session = ChatSession(user_id=current_user.id)
        db.add(session)
        db.commit()
        db.refresh(session)
    
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.timestamp.asc()).all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "sender_id": m.sender_id,
            "timestamp": m.timestamp,
            "is_admin": m.is_admin
        } for m in messages
    ]

@router.get("/admin/sessions/{session_id}/messages")
def get_session_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Admin only: get any session messages"""
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.timestamp.asc()).all()
    return [
        {
            "id": m.id,
            "content": m.content,
            "sender_id": m.sender_id,
            "timestamp": m.timestamp,
            "is_admin": m.is_admin
        } for m in messages
    ]

@router.post("/send")
def send_message(
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
    return {"status": "success"}

@router.post("/admin/reply")
def admin_reply(
    session_id: int,
    message: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    new_msg = ChatMessage(
        session_id=session_id,
        sender_id=current_user.id,
        content=message,
        is_admin=True
    )
    db.add(new_msg)
    db.commit()
    return {"status": "success"}
