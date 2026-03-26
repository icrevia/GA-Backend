from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from core.config import settings
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("zexplay")

from api.router import api_router
from core.database import engine, Base
from models import user, tournament, wallet, support

# ─────────────────────────────────────────────
# Rate limiter (global, keyed by IP)
# ─────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title=settings.PROJECT_NAME,
    # Disable public OpenAPI docs in production via env flag
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if settings.DEBUG else None,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─────────────────────────────────────────────
# Static Files
# ─────────────────────────────────────────────
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────────
# CORS — explicit origins only, never wildcard
# ─────────────────────────────────────────────
ALLOWED_ORIGINS = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ─────────────────────────────────────────────
# DB — create tables (no DROP, safe for prod)
# ─────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


@app.on_event("startup")
def startup_event():
    logger.info("ZexPlay API starting up...")
    logger.info(f"DEBUG mode: {settings.DEBUG}")
    logger.info(f"Allowed origins: {ALLOWED_ORIGINS}")


app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/")
def root():
    return {"message": "ZexPlay API — Online", "version": "2.0"}


@app.get("/api/v1/status")
def get_system_status():
    from core.database import SessionLocal
    from models.config import SystemConfig
    db = SessionLocal()
    try:
        configs = db.query(SystemConfig).all()
        config_map = {c.config_key: c.config_value for c in configs}
        maintenance_mode = config_map.get("maintenance_mode", "false").lower() == "true"
        return {
            "maintenance_mode": maintenance_mode,
            "status": "maintenance" if maintenance_mode else "online",
            "message": config_map.get(
                "maintenance_message",
                "Fine-tuning the gears for a smoother experience. We'll be back in just a blink!"
            ),
            "until": config_map.get("maintenance_until", ""),
            "latest_version_code": int(config_map.get("latest_version_code", "1")),
            "latest_version_name": config_map.get("latest_version_name", "1.0"),
            "update_url": config_map.get("update_url", ""),
            "force_update": config_map.get("force_update", "false").lower() == "true",
            "update_message": config_map.get(
                "update_message",
                "A new version of ZexPlay is available! Upgrade now for the latest features."
            )
        }
    finally:
        db.close()
