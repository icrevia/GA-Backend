from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import List
from core.database import get_db
from api.deps import get_current_user
from models.support import ChatSession, ChatMessage
from models.user import User
from core.websockets import manager
from datetime import datetime

router = APIRouter()

@router.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
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
            "sender_id": m.sender_id,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            "is_admin": m.is_admin
        } for m in messages
    ]

@router.get("/sessions", response_model=List[dict])
def get_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Filter: Only show sessions where USER still exists in the database
    sessions = db.query(ChatSession).join(User).filter(ChatSession.user_id == User.id).all()
    
    result = []
    for s in sessions:
        if not s.user:
            continue # Extra safety
            
        last_msg = db.query(ChatMessage).filter(ChatMessage.session_id == s.id).order_by(ChatMessage.timestamp.desc()).first()
        result.append({
            "id": s.id,
            "user_id": s.user_id,
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "user": {
                "username": s.user.username,
                "email": s.user.email
            },
            "last_message": last_msg.content if last_msg else "No messages yet",
            "last_timestamp": last_msg.timestamp.isoformat() if last_msg else s.created_at.isoformat() if s.created_at else None,
            "unread": 0
        })
    return result

@router.get("/my-chat", response_model=List[dict])
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
    return [
        {
            "id": m.id,
            "content": m.content,
            "sender_id": m.sender_id,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
            "is_admin": m.is_admin
        } for m in messages
    ]

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
        "sender_id": new_msg.sender_id,
        "is_admin": new_msg.is_admin,
        "timestamp": datetime.now().isoformat()
    }
    await manager.broadcast(msg_data)
    return {"status": "success"}

@router.post("/admin/reply")
async def admin_reply(
    session_id: int,
    message: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    new_msg = ChatMessage(
        session_id=session_id,
        sender_id=current_user.id,
        content=message,
        is_admin=True
    )
    db.add(new_msg)
    db.commit()
    
    msg_data = {
        "id": new_msg.id,
        "content": new_msg.content,
        "sender_id": new_msg.sender_id,
        "is_admin": True,
        "timestamp": datetime.now().isoformat()
    }
    await manager.broadcast(msg_data)
    return {"status": "success"}
