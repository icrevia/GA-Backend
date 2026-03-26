from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from core.config import settings
import os

from api.router import api_router

from core.database import engine, Base
from models import user, tournament, wallet, support

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# Static Files
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Startup DB migration/fix logic
@app.on_event("startup")
def startup_db_fix():
    from core.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            # Add missing columns if they don't exist
            conn.execute(text("ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS max_slots INTEGER DEFAULT 100"))
            conn.commit()
            print("DB Schema Migration: Checked/Fixed 'max_slots' column 🦾")
        except Exception as e:
            print(f"Non-critical migration skip: {str(e)}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DB — creates tables that don't exist, never drops
Base.metadata.create_all(bind=engine)

# Migration Helper: Auto-add columns if they are missing
def run_migrations():
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('tournaments')]
    
    with engine.connect() as conn:
        if 'match_type' not in columns:
            print("Migration: Adding match_type to tournaments. Status: PENDING")
            conn.execute(text("ALTER TABLE tournaments ADD COLUMN match_type VARCHAR(255) DEFAULT 'SOLO'"))
            conn.commit()
            print("Migration: Adding match_type to tournaments. Status: SUCCESS")
        
run_migrations()

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "ZexPlay API — Production Ready"}

@app.get("/api/v1/status")
def get_system_status():
    from core.database import SessionLocal
    from models.config import SystemConfig
    db = SessionLocal()
    try:
        maintenance = db.query(SystemConfig).filter(SystemConfig.config_key == "maintenance_mode").first()
        message = db.query(SystemConfig).filter(SystemConfig.config_key == "maintenance_message").first()
        until = db.query(SystemConfig).filter(SystemConfig.config_key == "maintenance_until").first()
        
        is_active = (maintenance.config_value.lower() == "true") if maintenance else False
        return {
            "maintenance_mode": is_active,
            "status": "maintenance" if is_active else "online",
            "message": message.config_value if message else "Fine-tuning the gears for a smoother experience. We'll be back in just a blink!",
            "until": until.config_value if until else ""
        }
    finally:
        db.close()
