from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from core.database import get_db
from core.security import hash_password
from models.user import User

router = APIRouter()

@router.post("/reset-admin")
def reset_admin_password(
    secret: str = Query(...),
    new_password: str = Query(...),
    db: Session = Depends(get_db)
):
    """One-time admin reset. Protected by secret key."""
    if secret != "ZEXPLAY_RESET_2024":
        return {"error": "Unauthorized"}
    
    admin = db.query(User).filter(User.email == "admin@zxtni.app").first()
    if not admin:
        # Create admin fresh
        admin = User(
            email="admin@zxtni.app",
            username="admin",
            hashed_password=hash_password(new_password),
            role="ADMIN",
            is_active=True
        )
        db.add(admin)
        db.commit()
        return {"status": "created", "email": "admin@zxtni.app", "password": new_password}
    
    # Update existing admin
    admin.hashed_password = hash_password(new_password)
    admin.role = "ADMIN"
    admin.is_active = True
    db.commit()
    return {"status": "updated", "email": admin.email, "role": admin.role}
