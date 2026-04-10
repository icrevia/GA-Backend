from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from core.config import settings
from services.login_security import extract_client_ip, is_ip_blocked
from contextlib import asynccontextmanager
import os
import uuid
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("GamerzAdda")

from api.router import api_router
from core.database import engine, Base
from models import user, tournament, wallet, support, withdraw_upi_account, promo, banner, restriction

# ─────────────────────────────────────────────
# Lifespan context manager (Modern approach)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    logger.info("GamerzAdda API starting up (Lifespan)...")
    async with engine.begin() as conn:
        # Create all tables asynchronously
        await conn.run_sync(Base.metadata.create_all)
        
        # Run safe migrations
        from sqlalchemy import text
        try:
            # Table migrations
            queries = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio VARCHAR(30)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(64)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_device VARCHAR(160)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(255) UNIQUE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_id INTEGER",
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_order_id VARCHAR(255)",
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_payment_id VARCHAR(255)",
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_signature VARCHAR(512)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS requires_admin BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_by_admin_id INTEGER",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_at TIMESTAMP",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS slot_no INTEGER",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_members TEXT",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tournament_participant_slot_idx ON tournament_participants (tournament_id, slot_no) WHERE slot_no IS NOT NULL"
            ]
            for query in queries:
                await conn.execute(text(query))
            await conn.commit()
            logger.info("DB sync & migration successful")
        except Exception as e:
            logger.warning(f"DB migration partial failure: {e}")
            
    yield
    # SHUTDOWN
    logger.info("GamerzAdda API shutting down...")

# rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if settings.DEBUG else None,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"rid={request_id} global_error={str(exc)}", exc_info=True)
    return Response(
        content='{"detail": "An internal server error occurred."}',
        status_code=500,
        media_type="application/json"
    )

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
        if not settings.DEBUG:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(f"rid={request_id} {request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms")
        return response

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)

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

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": "GamerzAdda API — Online", "version": "2.0"}

@app.get("/api/v1/status")
async def get_system_status():
    from core.database import SessionLocal
    from models.config import SystemConfig
    from sqlalchemy import select
    async with SessionLocal() as db:
        try:
            result = await db.execute(select(SystemConfig))
            configs = result.scalars().all()
            config_map = {c.config_key: c.config_value for c in configs}
            return {
                "maintenance_mode": config_map.get("maintenance_mode", "false").lower() == "true",
                "status": "online",
                "latest_version_code": int(config_map.get("latest_version_code", "1")),
                "support_email": "support@gamerzadda.in"
            }
        except Exception as e:
            return {"status": "degraded", "error": str(e)}
