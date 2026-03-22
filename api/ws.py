from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from typing import Optional
from jose import jwt, JWTError

from core.websockets import manager
from core.config import settings

router = APIRouter()

async def get_user_id_from_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return int(user_id)
    except JWTError:
        return None

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str):
    user_id = await get_user_id_from_token(token)
    if not user_id:
        await websocket.close(code=1008) # Policy violation (unauthorized)
        return

    await manager.connect(user_id, websocket)
    try:
        while True:
            # We don't necessarily need to handle incoming messages from frontend
            # other than generic pings or acks.
            data = await websocket.receive_text()
            # Respond to ping
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
