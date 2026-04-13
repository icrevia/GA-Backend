import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from core.websockets import manager
from services.push_notifications import send_push, send_push_to_many
from models.user import User

logger = logging.getLogger("GamerzAdda.support_notifications")

async def notify_support_message(
    db: AsyncSession,
    user_id: int,
    msg_data: dict,
    notify_via_push: bool = True
):
    """
    Sends a chat message to a specific user via WebSocket.
    If they are offline, fallback to a Push Notification.
    """
    # 1. Attempt WebSocket Delivery
    sent_via_ws = await manager.send_personal_message(msg_data, user_id)
    
    if sent_via_ws or not notify_via_push:
        return True

    # 2. WebSocket Failed -> Fallback to Push Notification
    # Only send push if the sender is an ADMIN (avoid pushing own messages back to sender)
    # OR if it's an auto-reply.
    if not msg_data.get("is_admin"):
        return False

    user_result = await db.execute(select(User.fcm_token).where(User.id == user_id))
    fcm_token = user_result.scalar_one_or_none()
    
    if fcm_token:
        title = "Support Reply"
        content = msg_data.get("content", "New message from support")
        # Truncate content for notification body
        body = (content[:100] + "...") if len(content) > 100 else content
        
        success = send_push(
            fcm_token=fcm_token,
            title=title,
            body=body,
            data={"type": "support_chat", "session_id": msg_data.get("session_id", "")}
        )
        if success:
            logger.info(f"Support push fallback successful for user_id={user_id}")
        return success
    
    return False

async def notify_admin_escalation(
    db: AsyncSession,
    escalation_data: dict,
    msg_data: dict = None
):
    """
    Broadcasts a support escalation/new message to all online admins.
    Also sends a push notification to ALL known admins with FCM tokens.
    """
    # 1. Real-time broadcast to connected admins
    await manager.broadcast_to_admins(escalation_data)
    if msg_data:
        await manager.broadcast_to_admins(msg_data)

    # 2. Push Notification to all admins
    # Performance Optimization: Cache admin tokens if user base grows large.
    admin_tokens_result = await db.execute(
        select(User.fcm_token).where(User.role == "ADMIN").where(User.fcm_token != None)
    )
    fcm_tokens = [t for (t,) in admin_tokens_result.all() if t]
    
    if fcm_tokens:
        user_name = escalation_data.get("user_id", "A user") # Ideally fetch real username
        title = f"New Support Message"
        preview = escalation_data.get("preview", "Check admin panel")
        body = f"User needs help: {preview}"
        
        send_push_to_many(
            fcm_tokens=fcm_tokens,
            title=title,
            body=body,
            data={"type": "admin_support_alert", "session_id": escalation_data.get("session_id", "")}
        )
        logger.info(f"Support escalation push sent to {len(fcm_tokens)} admins")
