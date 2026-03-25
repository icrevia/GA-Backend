from fastapi import WebSocket
from typing import Dict, List
import json

CALL_SIGNAL_TYPES = {"call_ring", "call_accepted", "call_rejected", "call_end", "offer", "answer", "ice_candidate", "admin_call_request"}

class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
        self.active_connections: Dict[int, List[WebSocket]] = {}
        # Admin sockets (user_id of admins stored separately for quick routing)
        self.admin_connections: List[WebSocket] = []

    async def connect(self, user_id: int, websocket: WebSocket, is_admin: bool = False):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        if is_admin and websocket not in self.admin_connections:
            self.admin_connections.append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if len(self.active_connections[user_id]) == 0:
                del self.active_connections[user_id]
        if websocket in self.admin_connections:
            self.admin_connections.remove(websocket)

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            dead = []
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    dead.append(connection)
            for d in dead:
                self.active_connections[user_id].remove(d)

    async def broadcast_to_admins(self, message: dict):
        dead = []
        for connection in self.admin_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                dead.append(connection)
        for d in dead:
            self.admin_connections.remove(d)

    async def broadcast(self, message: dict):
        for user_id, connections in self.active_connections.items():
            for connection in connections:
                await connection.send_text(json.dumps(message))

    def is_admin_online(self) -> bool:
        return len(self.admin_connections) > 0

manager = ConnectionManager()
