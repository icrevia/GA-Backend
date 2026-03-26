from fastapi import WebSocket
from typing import Dict, List, Optional
import json

CALL_SIGNAL_TYPES = {
    "call_ring", "call_accepted", "call_rejected", "call_end",
    "offer", "answer", "ice_candidate", "admin_call_request",
    "call_busy", "chat_message", "call_hold", "call_unhold"
}

class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
        self.active_connections: Dict[int, List[WebSocket]] = {}
        # Admin sockets for quick routing
        self.admin_connections: List[WebSocket] = []
        # admin_user_id -> user_id: tracks which user each admin is in a call with
        self.active_call_map: Dict[int, int] = {}
        # user_id -> admin_user_id: reverse map
        self.user_to_admin_map: Dict[int, int] = {}

    async def connect(self, user_id: int, websocket: WebSocket, is_admin: bool = False):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        if is_admin and websocket not in self.admin_connections:
            self.admin_connections.append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            try:
                self.active_connections[user_id].remove(websocket)
            except ValueError:
                pass
            if len(self.active_connections[user_id]) == 0:
                del self.active_connections[user_id]
        if websocket in self.admin_connections:
            try:
                self.admin_connections.remove(websocket)
            except ValueError:
                pass

    def set_call_pair(self, admin_user_id: int, target_user_id: int):
        """Track that an admin is now in a call with a specific user."""
        self.active_call_map[admin_user_id] = target_user_id
        self.user_to_admin_map[target_user_id] = admin_user_id

    def clear_call_pair_by_admin(self, admin_user_id: int):
        """Clear call pair by admin user ID."""
        user_id = self.active_call_map.pop(admin_user_id, None)
        if user_id is not None:
            self.user_to_admin_map.pop(user_id, None)

    def clear_call_pair_by_user(self, user_id: int):
        """Clear call pair by regular user ID."""
        admin_id = self.user_to_admin_map.pop(user_id, None)
        if admin_id is not None:
            self.active_call_map.pop(admin_id, None)

    def get_user_for_admin(self, admin_user_id: int) -> Optional[int]:
        return self.active_call_map.get(admin_user_id)

    def get_admin_for_user(self, user_id: int) -> Optional[int]:
        return self.user_to_admin_map.get(user_id)

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            dead = []
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    dead.append(connection)
            for d in dead:
                try:
                    self.active_connections[user_id].remove(d)
                except ValueError:
                    pass

    async def broadcast_to_admins(self, message: dict):
        dead = []
        for connection in self.admin_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                dead.append(connection)
        for d in dead:
            try:
                self.admin_connections.remove(d)
            except ValueError:
                pass

    async def broadcast(self, message: dict):
        for user_id, connections in list(self.active_connections.items()):
            for connection in connections:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    pass

    def is_admin_online(self) -> bool:
        return len(self.admin_connections) > 0

manager = ConnectionManager()
