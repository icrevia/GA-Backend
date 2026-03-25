from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Optional
from jose import jwt, JWTError
import json

from core.websockets import manager, CALL_SIGNAL_TYPES
from core.config import settings
from core.database import SessionLocal
from models.user import User

router = APIRouter()

async def get_user_from_token(token: str):
    """Returns (user_id, is_admin) or (None, False)"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            return None, False
        uid = int(user_id)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == uid).first()
            is_admin = user.role == "ADMIN" if user else False
        finally:
            db.close()
        return uid, is_admin
    except JWTError:
        return None, False

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str):
    user_id, is_admin = await get_user_from_token(token)
    if not user_id:
        await websocket.close(code=1008)
        return

    await manager.connect(user_id, websocket, is_admin=is_admin)
    try:
        while True:
            data = await websocket.receive_text()

            if data == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type not in CALL_SIGNAL_TYPES:
                continue

            if is_admin:
                # Admin → route to specific user
                target_user_id = msg.get("to_user_id")
                if target_user_id:
                    msg["from"] = "admin"
                    await manager.send_personal_message(msg, int(target_user_id))
            else:
                # User → route to all admins, include caller info
                msg["from_user_id"] = user_id
                await manager.broadcast_to_admins(msg)

    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        # Notify the other side if a call was active
        if is_admin:
            pass  # Admin left — couldn't easily know which user to notify
        else:
            await manager.broadcast_to_admins({"type": "call_end", "from_user_id": user_id, "reason": "disconnected"})
