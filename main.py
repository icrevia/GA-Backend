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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DB — creates tables that don't exist, never drops
Base.metadata.create_all(bind=engine)

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
        return {
            "maintenance_mode": maintenance.config_value == "True" if maintenance else False,
            "status": "online" if (not maintenance or maintenance.config_value == "False") else "maintenance"
        }
    finally:
        db.close()
