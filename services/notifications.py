from sqlalchemy.orm import Session
from models.notification import Notification

def add_user_notification(db: Session, user_id: int, title: str, content: str, type: str = "APP"):
    """Easily create a user-specific notification."""
    notif = Notification(
        user_id=user_id,
        title=title,
        content=content,
        type=type
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif
