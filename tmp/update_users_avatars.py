from sqlalchemy.orm import Session
from core.database import engine, SessionLocal
from models.user import User
from core.config import settings

def update_avatars():
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.profile_pic == None).all()
        print(f"Found {len(users)} users without profile_pic.")
        
        for user in users:
            avatar_id = (user.id % 5) + 1
            user.profile_pic = f"{settings.APP_URL}/static/avatars/avatar{avatar_id}.png"
            print(f"Assigning avatar {avatar_id} to user {user.username}")
        
        db.commit()
        print("Successfully updated avatars for existing users.")
    except Exception as e:
        print(f"Error updating avatars: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    update_avatars()
