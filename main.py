from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.config import settings
from api.router import api_router

from core.database import engine, Base
from models import user, tournament, wallet, support

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DB — creates tables that don't exist, never drops
Base.metadata.create_all(bind=engine)

# EMERGENCY: Forced Password Overwrite (Runs only on startup)
from core.database import SessionLocal
from core.security import hash_password
from models.user import User

def reset_admin():
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@zxtni.app").first()
        if admin:
            admin.hashed_password = hash_password("rahulxd") # FORCING known good password
            db.commit()
            print("EMERGENCY: admin@zxtni.app password correctly re-hashed to 'rahulxd'")
    except Exception as e:
        print(f"RESET ERROR: {e}")
    finally:
        db.close()

reset_admin()

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "ZexPlay API — Production Ready"}
