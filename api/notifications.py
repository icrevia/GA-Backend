from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta

from api.deps import get_current_user_profile
from core.database import get_db_sync as get_db
from models.user import User
from models.notification import Notification
from schemas.notification import NotificationResponse

router = APIRouter()

@router.get("/", response_model=List[NotificationResponse])
def get_user_notifications(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_profile)):
    """Fetch all notifications for the current user, auto-removing those older than 24 hours."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    db.query(Notification).filter(Notification.user_id == current_user.id, Notification.created_at < cutoff).delete()
    db.commit()
    
    return db.query(Notification).filter(Notification.user_id == current_user.id).order_by(Notification.created_at.desc()).limit(50).all()

@router.post("/read-all")
def mark_all_as_read(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_profile)):
    """Mark all unread notifications as read."""
    db.query(Notification).filter(Notification.user_id == current_user.id, Notification.is_read == False).update({"is_read": True})
    db.commit()
    return {"message": "All notifications marked as read."}

@router.delete("/clear-all")
def clear_all_notifications(db: Session = Depends(get_db), current_user: User = Depends(get_current_user_profile)):
    """Delete all notifications for the current user."""
    db.query(Notification).filter(Notification.user_id == current_user.id).delete()
    db.commit()
    return {"message": "All notifications cleared."}
