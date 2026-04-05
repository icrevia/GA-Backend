from fastapi import WebSocket
from typing import Dict, List, Optional
import json
import logging
from collections import deque

logger = logging.getLogger("GamerzAdda.ws")

CALL_SIGNAL_TYPES = {
    "call_ring", "call_accepted", "call_rejected", "call_end",
    "offer", "answer", "ice_candidate", "admin_call_request",
    "call_busy", "chat_message", "call_hold", "call_unhold"
}

SUPPORT_EVENT_TYPES = {"chat_message", "support_escalation", "support_attended", "support_unattended"}
MAX_PENDING_ADMIN_SUPPORT_EVENTS = 200


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
        # Buffer support events when no admins are online so reconnecting admin panels can catch up.
        self.pending_admin_support_events: deque[dict] = deque(maxlen=MAX_PENDING_ADMIN_SUPPORT_EVENTS)

    async def connect(self, user_id: int, websocket: WebSocket, is_admin: bool = False):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        if is_admin and websocket not in self.admin_connections:
            self.admin_connections.append(websocket)

            if self.pending_admin_support_events:
                replayed = 0
                while self.pending_admin_support_events:
                    queued_event = self.pending_admin_support_events[0]
                    try:
                        await websocket.send_text(json.dumps(queued_event))
                        replayed += 1
                        self.pending_admin_support_events.popleft()
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

    def set_call_pair(self, admin_user_id: int, target_user_id: int):
        """Track that an admin is now in a call with a specific user."""
        self.active_call_map[admin_user_id] = target_user_id
        self.user_to_admin_map[target_user_id] = admin_user_id
        logger.info(f"WS CallPair set: Admin={admin_user_id} <-> User={target_user_id}")

    def clear_call_pair_by_admin(self, admin_user_id: int):
        user_id = self.active_call_map.pop(admin_user_id, None)
        if user_id is not None:
            self.user_to_admin_map.pop(user_id, None)
        logger.info(f"WS CallPair cleared for Admin={admin_user_id}")

    def clear_call_pair_by_user(self, user_id: int):
        admin_id = self.user_to_admin_map.pop(user_id, None)
        if admin_id is not None:
            self.active_call_map.pop(admin_id, None)
        logger.info(f"WS CallPair cleared for User={user_id}")

    def get_user_for_admin(self, admin_user_id: int) -> Optional[int]:
        return self.active_call_map.get(admin_user_id)

    def get_admin_for_user(self, user_id: int) -> Optional[int]:
        return self.user_to_admin_map.get(user_id)

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
        for user_id, connections in list(self.active_connections.items()):
            for connection in list(connections):
                try:
                    await connection.send_text(json.dumps(message))
                except Exception:
                    pass


manager = ConnectionManager()
