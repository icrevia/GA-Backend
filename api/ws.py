import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
import logging
from sqlalchemy import select

from core.websockets import manager, ALLOWED_WS_EVENTS
from core.security import decode_access_token
from core.database import SessionLocal
from models.user import User

logger = logging.getLogger("GamerzAdda.ws")
router = APIRouter()


def _extract_ws_token_and_protocol(websocket: WebSocket) -> tuple[str | None, str | None]:
    """
    Extract auth token from websocket handshake without using query parameters.
    Supports:
    - Authorization: Bearer <jwt>
    - Sec-WebSocket-Protocol: GamerzAdda.v1, token.<jwt>
    """
    raw_protocols = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [p.strip() for p in raw_protocols.split(",") if p.strip()]

    selected_protocol = None
    for proto in protocols:
        if proto.lower() == "gamerzadda.v1":
            selected_protocol = "gamerzadda.v1"
            break

    for proto in protocols:
        if proto.lower().startswith("token."):
            token = proto[len("token."):].strip()
            return token or None, selected_protocol

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        return token or None, selected_protocol

    token_qs = websocket.query_params.get("token")
    if token_qs and token_qs not in ("null", "undefined", ""):
        return token_qs.strip(), selected_protocol

    return None, selected_protocol


async def get_user_from_token(token: str) -> tuple[int | None, bool, str | None]:
    """Decode JWT and return (user_id, is_admin, username)."""
    if not token or token in ("null", "undefined", ""):
        logger.warning("WS Auth: Token is empty or null")
        return None, False, None

    try:
        payload = decode_access_token(token)
    except Exception as e:
        logger.warning(f"WS Auth Token Decode Error: {e}")
        return None, False, None

    user_id = payload.get("sub")
    if user_id is None:
        logger.warning("WS Auth: No 'sub' in token payload")
        return None, False, None

    uid = int(user_id)

    try:
        async with SessionLocal() as db:
            # Keep websocket auth responsive under pool pressure.
            user_row = await asyncio.wait_for(
                db.execute(
                    select(
                        User.id,
                        User.role,
                        User.username,
                        User.is_active,
                        User.token_version,
                    ).where(User.id == uid)
                ),
                timeout=5,
            )
            row = user_row.first()

        if not row:
            logger.warning(f"WS Auth: user_id={uid} not found in DB")
            return None, False, None

        token_version = payload.get("tv", 0)
        db_token_version = row.token_version or 0
        if not row.is_active:
            logger.warning(f"WS Auth: user_id={uid} is banned")
            return None, False, None

        if int(token_version) != int(db_token_version):
            logger.warning(f"WS Auth: user_id={uid} token version mismatch")
            return None, False, None

        is_admin = (row.role == "ADMIN")
        username = row.username
        return uid, is_admin, username
    except asyncio.TimeoutError:
        logger.warning("WS Auth DB timeout while checking token")
        return None, False, None
    except Exception as e:
        logger.error(f"WS Auth DB Error: {e}")
        return None, False, None


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token, selected_protocol = _extract_ws_token_and_protocol(websocket)

    # Accept first to avoid ASGI proxy rejections, then verify token
    await websocket.accept(subprotocol=selected_protocol)

    user_id, is_admin, username = await get_user_from_token(token)
    if not user_id:
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Authentication failed. Invalid or missing token."
            }))
        except Exception:
            pass
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    await manager.connect(user_id, websocket, is_admin=is_admin)

    # Notify the connecting client that they're registered
    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "user_id": user_id,
            "is_admin": is_admin
        }))
    except Exception:
        manager.disconnect(user_id, websocket)
        return

    try:
        while True:
            data = await websocket.receive_text()

            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            logger.info(f"WS Signal: From={user_id} Type={msg_type} IsAdmin={is_admin}")

            if msg_type not in ALLOWED_WS_EVENTS:
                logger.debug(f"WS Signal: Unknown type={msg_type} from user_id={user_id}")
                continue

            if is_admin:
                target_user_id = msg.get("to_user_id")
                if not target_user_id:
                    logger.warning(f"WS Admin signal missing to_user_id: type={msg_type}")
                    continue

                target_user_id = int(target_user_id)
                msg["from_user_id"] = user_id
                msg["from"] = "admin"

                delivered = await manager.send_personal_message(msg, target_user_id)
                if not delivered:
                    logger.warning(
                        f"WS Message NOT delivered: Admin={user_id} -> User={target_user_id} "
                        f"type={msg_type} (user has no live socket)"
                    )

            else:
                msg["from_user_id"] = user_id
                msg["from_user_name"] = username or f"User #{user_id}"

                await manager.broadcast_to_admins(msg)

    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
    except Exception as e:
        logger.warning(f"WS runtime error for user_id={user_id}: {e}")
        manager.disconnect(user_id, websocket)
