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
    if not token or token == "null" or token == "undefined":
        print("WebSocket Auth: Token is empty or null string")
        return None, False

    try:
        from core.config import settings
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            print("WebSocket Auth: No 'sub' in token payload")
            return None, False
            
        uid = int(user_id)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == uid).first()
            is_admin = (user.role == "ADMIN") if user else False
            return uid, is_admin
        except Exception as e:
            print(f"WebSocket DB Error: {e}")
            return uid, False
        finally:
            db.close()
    except Exception as e:
        print(f"WebSocket Token Decode Error: {e}")
        return None, False

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    # Accept the connection FIRST to prevent HTTP 403 Forbidden handshake rejections by ASGI proxy
    await websocket.accept()
    
    user_id, is_admin = await get_user_from_token(token)
    if not user_id:
        await websocket.send_text(json.dumps({"type": "error", "message": "Authentication failed. Invalid or missing token."}))
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

            print(f"WS Signal: From={user_id} Type={msg_type} IsAdmin={is_admin}")

            if msg_type not in CALL_SIGNAL_TYPES:
                print(f"WS Signal: Rejected Type={msg_type}")
                continue

            if is_admin:
                # Admin → route to specific user
                target_user_id = msg.get("to_user_id")
                if target_user_id:
                    target_user_id = int(target_user_id)
                    # Stamp admin's user_id so Android can identify which admin sent this
                    msg["from_user_id"] = user_id
                    msg["from"] = "admin"

                    # Track call pairing when admin sends an offer or initiates a call
                    if msg_type in ("offer", "admin_call_request"):
                        manager.set_call_pair(user_id, target_user_id)
                        print(f"WS CallPair: Admin={user_id} ↔ User={target_user_id}")

                    # Clear call pairing when call ends
                    if msg_type in ("call_end", "call_rejected"):
                        manager.clear_call_pair_by_admin(user_id)
                        print(f"WS CallPair cleared for Admin={user_id}")

                    await manager.send_personal_message(msg, target_user_id)
            else:
                # User → route to all admins, include caller info
                msg["from_user_id"] = user_id
                # Get username for convenience
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.id == user_id).first()
                    msg["from_user_name"] = user.username if user else f"User #{user_id}"
                finally:
                    db.close()

                # Clear call pairing when user ends/rejects
                if msg_type in ("call_end", "call_rejected"):
                    manager.clear_call_pair_by_user(user_id)
                    print(f"WS CallPair cleared for User={user_id}")

                await manager.broadcast_to_admins(msg)

    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        if is_admin:
            # Only notify the specific user this admin was in a call with (not ALL users!)
            paired_user_id = manager.get_user_for_admin(user_id)
            manager.clear_call_pair_by_admin(user_id)
            if paired_user_id:
                print(f"WS: Admin {user_id} disconnected, notifying only user {paired_user_id}")
                await manager.send_personal_message(
                    {"type": "call_end", "from_user_id": user_id, "from": "admin", "reason": "admin_disconnected"},
                    paired_user_id
                )
        else:
            # User disconnected — notify admins
            manager.clear_call_pair_by_user(user_id)
            await manager.broadcast_to_admins(
                {"type": "call_end", "from_user_id": user_id, "reason": "user_disconnected"}
            )
