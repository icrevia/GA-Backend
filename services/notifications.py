import time
import threading
import logging
from sqlalchemy.orm import Session
from models.user import User
from models.notification import Notification
from services.notification_text import append_firebase_suffix
from services.push_notifications import send_push

logger = logging.getLogger(__name__)

def add_user_notification(
    db: Session,
    user_id: int,
    title: str,
    content: str,
    type: str = "APP",
    delay_push_seconds: int = 0,
):
    """Easily create a user-specific notification.

    Args:
        delay_push_seconds: If > 0, the push notification (FCM) is delayed
            by this many seconds. The in-app notification record is still
            saved immediately. Useful for spin rewards where the wheel
            animation needs to finish before the user sees the alert.
    """
    display_title = append_firebase_suffix(title, max_length=100)
    display_content = append_firebase_suffix(content)

    notif = Notification(
        user_id=user_id,
        title=display_title,
        content=display_content,
        type=type
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    # ── Push Notification Trigger (Background) ───────────────────
    try:
        # Fetch token from the current session (fast)
        user = db.query(User).filter(User.id == user_id).first()
        fcm_token = user.fcm_token if user else None

        if fcm_token:
            def _bg_push():
                try:
                    if delay_push_seconds > 0:
                        time.sleep(delay_push_seconds)
                    send_push(
                        fcm_token=fcm_token,
                        title=display_title,
                        body=display_content,
                        data={"type": type, "notification_id": str(notif.id)}
                    )
                except Exception as e:
                    logger.warning(f"Background push failed for user {user_id}: {e}")

            threading.Thread(target=_bg_push, daemon=True).start()
    except Exception as outer_err:
        logger.warning(f"FCM trigger skipped for user {user_id}: {outer_err}")
    # ─────────────────────────────────────────────────────────────

    return notif
