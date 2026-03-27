from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import jwt, JWTError
import json
import logging

from core.websockets import manager, CALL_SIGNAL_TYPES
from core.config import settings
from core.database import SessionLocal
from models.user import User

logger = logging.getLogger("zexplay.ws")
router = APIRouter()


async def get_user_from_token(token: str):
    """Decode JWT and return (user_id, is_admin) or (None, False)."""
    if not token or token in ("null", "undefined", ""):
        logger.warning("WS Auth: Token is empty or null")
        return None, False

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            logger.warning("WS Auth: No 'sub' in token payload")
            return None, False

        uid = int(user_id)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == uid).first()
            if not user:
                logger.warning(f"WS Auth: user_id={uid} not found in DB")
                return None, False
            if not user.is_active:
                logger.warning(f"WS Auth: user_id={uid} is banned")
                return None, False
            is_admin = (user.role == "ADMIN")
            return uid, is_admin
        except Exception as e:
            logger.error(f"WS Auth DB Error: {e}")
            return uid, False
        finally:
            db.close()
    except JWTError as e:
        logger.warning(f"WS Auth Token Decode Error: {e}")
        return None, False


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    # Accept first to avoid ASGI proxy rejections, then verify token
    await websocket.accept()

    user_id, is_admin = await get_user_from_token(token)
    if not user_id:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Authentication failed. Invalid or missing token."
        }))
        await websocket.close(code=1008)
        return

    await manager.connect(user_id, websocket, is_admin=is_admin)

    # Notify the connecting client that they're registered
    await websocket.send_text(json.dumps({
        "type": "connected",
        "user_id": user_id,
        "is_admin": is_admin
    }))

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
            logger.info(f"WS Signal: From={user_id} Type={msg_type} IsAdmin={is_admin}")

            if msg_type not in CALL_SIGNAL_TYPES:
                logger.debug(f"WS Signal: Unknown type={msg_type} from user_id={user_id}")
                continue

            if is_admin:
                # ── Admin → route to a specific user ─────────────────────────
                target_user_id = msg.get("to_user_id")
                if not target_user_id:
                    logger.warning(f"WS Admin signal missing to_user_id: type={msg_type}")
                    continue

                target_user_id = int(target_user_id)
                msg["from_user_id"] = user_id
                msg["from"] = "admin"

                # Check if target user is actually connected BEFORE trying to send
                if not manager.is_user_online(target_user_id):
                    logger.warning(
                        f"WS Call failed: Admin={user_id} tried to call User={target_user_id} "
                        f"but user is NOT connected to WebSocket"
                    )
                    # Notify admin that user is offline
                    await websocket.send_text(json.dumps({
                        "type": "call_end",
                        "reason": "user_offline",
                        "to_user_id": target_user_id,
                        "message": "User is not connected"
                    }))
                    continue

                # Track call pairing when admin initiates
                if msg_type in ("offer", "admin_call_request"):
                    manager.set_call_pair(user_id, target_user_id)

                # Clear call pairing when call ends
                if msg_type in ("call_end", "call_rejected"):
                    manager.clear_call_pair_by_admin(user_id)

                delivered = await manager.send_personal_message(msg, target_user_id)
                if not delivered:
                    logger.warning(
                        f"WS Message NOT delivered: Admin={user_id} -> User={target_user_id} "
                        f"type={msg_type} (user has no live socket)"
                    )
                    await websocket.send_text(json.dumps({
                        "type": "call_end",
                        "reason": "user_offline",
                        "to_user_id": target_user_id,
                        "message": "Could not reach user"
                    }))

            else:
                # ── User → route to all admins ────────────────────────────────
                msg["from_user_id"] = user_id

                # Fetch username for admin-side display
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.id == user_id).first()
                    msg["from_user_name"] = user.username if user else f"User #{user_id}"
                finally:
                    db.close()

                # Check if ANY admin is online before sending call signal
                if msg_type == "admin_call_request" and not manager.is_admin_online():
                    logger.warning(f"WS: User={user_id} tried to call but no admin is online")
                    await websocket.send_text(json.dumps({
                        "type": "call_end",
                        "reason": "no_admin_online",
                        "message": "No admin is currently available"
                    }))
                    continue

                # Clear call pairing when user ends/rejects
                if msg_type in ("call_end", "call_rejected"):
                    manager.clear_call_pair_by_user(user_id)

                await manager.broadcast_to_admins(msg)

    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        if is_admin:
            paired_user_id = manager.get_user_for_admin(user_id)
            manager.clear_call_pair_by_admin(user_id)
            if paired_user_id:
                logger.info(f"WS: Admin {user_id} disconnected mid-call, notifying User={paired_user_id}")
                await manager.send_personal_message(
                    {"type": "call_end", "from_user_id": user_id, "from": "admin", "reason": "admin_disconnected"},
                    paired_user_id
                )
        else:
            manager.clear_call_pair_by_user(user_id)
            await manager.broadcast_to_admins(
                {"type": "call_end", "from_user_id": user_id, "reason": "user_disconnected"}
            )
