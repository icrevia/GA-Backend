from fastapi import WebSocket
from typing import Dict, List, Optional
import json
import logging
from collections import deque

logger = logging.getLogger("GamerzAdda.ws")

ALLOWED_WS_EVENTS = {
    "chat_message", "support_escalation", "support_activity",
    "join_quiz", "leave_quiz", "quiz_answer", "quiz_sync", "quiz_surrender"
}

SUPPORT_EVENT_TYPES = {
    "chat_message",
    "support_escalation",
    "support_blocked",
    "support_unblocked",
    "support_thread_updated",
}
MAX_PENDING_ADMIN_SUPPORT_EVENTS = 200


class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
        self.active_connections: Dict[int, List[WebSocket]] = {}
        # Admin sockets for quick routing
        self.admin_connections: List[WebSocket] = []
        # admin_connections: List[WebSocket] already declared
        # Buffer support events when no admins are online so reconnecting admin panels can catch up.
        self.pending_admin_support_events: deque[dict] = deque(maxlen=MAX_PENDING_ADMIN_SUPPORT_EVENTS)
        # quiz_id -> Set[user_id]
        self.quiz_rooms: Dict[int, set[int]] = {}

    async def connect(self, user_id: int, websocket: WebSocket, is_admin: bool = False):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        if is_admin and websocket not in self.admin_connections:
            self.admin_connections.append(websocket)

            if self.pending_admin_support_events:
                replayed = 0
                for queued_event in list(self.pending_admin_support_events):
                    try:
                        await websocket.send_text(json.dumps(queued_event))
                        replayed += 1
                    except Exception as e:
                        logger.warning(f"WS admin replay failed for user_id={user_id}: {e}")
                        break

                if replayed > 0:
                    logger.info(
                        f"WS replayed {replayed} pending support events to admin user_id={user_id}"
                    )

        logger.info(
            f"WS connect: user_id={user_id} is_admin={is_admin} "
            f"total_users={len(self.active_connections)} "
            f"total_admins={len(self.admin_connections)}"
        )

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
        logger.info(
            f"WS disconnect: user_id={user_id} "
            f"total_users={len(self.active_connections)} "
            f"total_admins={len(self.admin_connections)}"
        )
        # Remove from any quiz rooms
        for qid in list(self.quiz_rooms.keys()):
            self.quiz_rooms[qid].discard(user_id)
            if not self.quiz_rooms[qid]:
                del self.quiz_rooms[qid]



    def is_user_online(self, user_id: int) -> bool:
        """Check if a user has at least one active WebSocket connection."""
        return user_id in self.active_connections and len(self.active_connections[user_id]) > 0

    def is_admin_online(self) -> bool:
        return len(self.admin_connections) > 0

    async def send_personal_message(self, message: dict, user_id: int) -> bool:
        """
        Send a message to a specific user. Returns True if at least one socket received it.
        Dead sockets are automatically pruned.
        """
        if user_id not in self.active_connections:
            logger.warning(f"WS send_personal_message: user_id={user_id} is NOT connected — message dropped: {message.get('type')}")
            return False

        sent = False
        dead = []
        for connection in list(self.active_connections[user_id]):
            try:
                await connection.send_text(json.dumps(message))
                sent = True
            except Exception as e:
                logger.warning(f"WS dead socket for user_id={user_id}: {e}")
                dead.append(connection)

        for d in dead:
            try:
                self.active_connections[user_id].remove(d)
            except ValueError:
                pass
        if dead and not self.active_connections.get(user_id):
            del self.active_connections[user_id]

        if not sent:
            logger.warning(f"WS send_personal_message: All sockets dead for user_id={user_id}, message NOT delivered: {message.get('type')}")
        return sent

    async def broadcast_to_admins(self, message: dict):
        """Broadcast a message to all connected admin sockets."""
        msg_type = message.get("type")

        if not self.admin_connections:
            if msg_type in SUPPORT_EVENT_TYPES:
                self.pending_admin_support_events.append(dict(message))
                logger.info(
                    "WS broadcast_to_admins: no admins connected — queued support event type=%s pending=%s",
                    msg_type,
                    len(self.pending_admin_support_events),
                )
            else:
                logger.debug(f"WS broadcast_to_admins: no admins connected — message dropped: {msg_type}")
            return

        dead = []
        delivered = 0
        for connection in list(self.admin_connections):
            try:
                await connection.send_text(json.dumps(message))
                delivered += 1
            except Exception as e:
                logger.warning(f"WS dead admin socket: {e}")
                dead.append(connection)
        for d in dead:
            try:
                self.admin_connections.remove(d)
            except ValueError:
                pass

        if msg_type in SUPPORT_EVENT_TYPES and delivered == 0:
            self.pending_admin_support_events.append(dict(message))
            logger.info(
                "WS broadcast_to_admins: support event type=%s queued after zero delivery pending=%s",
                msg_type,
                len(self.pending_admin_support_events),
            )

    async def broadcast(self, message: dict):
        """Broadcast to ALL connected users (admin + regular)."""
        dead_users = set()
        for user_id, connections in list(self.active_connections.items()):
            dead_sockets = []
            for connection in list(connections):
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    dead_sockets.append(connection)
            for d in dead_sockets:
                try:
                    self.active_connections[user_id].remove(d)
                except ValueError:
                    pass
            if not self.active_connections[user_id]:
                dead_users.add(user_id)
        for u in dead_users:
            self.active_connections.pop(u, None)

    async def force_logout_user(self, user_id: int, reason: str = "Session revoked"):
        """Close all sockets for a user with policy-violation code to trigger client logout."""
        sockets = list(self.active_connections.get(user_id, []))
        if not sockets:
            return

        safe_reason = (reason or "Session revoked")[:120]
        for socket in sockets:
            try:
                await socket.close(code=1008, reason=safe_reason)
            except Exception:
                pass

            if socket in self.admin_connections:
                try:
                    self.admin_connections.remove(socket)
                except ValueError:
                    pass

        self.active_connections.pop(user_id, None)

    async def join_quiz_room(self, user_id: int, quiz_id: int):
        if quiz_id not in self.quiz_rooms:
            self.quiz_rooms[quiz_id] = set()
        self.quiz_rooms[quiz_id].add(user_id)
        logger.info(f"WS Quiz: User {user_id} joined room {quiz_id}. Total: {len(self.quiz_rooms[quiz_id])}")

    async def leave_quiz_room(self, user_id: int, quiz_id: int):
        if quiz_id in self.quiz_rooms:
            self.quiz_rooms[quiz_id].discard(user_id)
            if not self.quiz_rooms[quiz_id]:
                del self.quiz_rooms[quiz_id]
            logger.info(f"WS Quiz: User {user_id} left room {quiz_id}")

    async def broadcast_to_quiz(self, quiz_id: int, message: dict):
        if quiz_id not in self.quiz_rooms:
            return
        user_ids = list(self.quiz_rooms[quiz_id])
        for user_id in user_ids:
            await self.send_personal_message(message, user_id)


manager = ConnectionManager()
