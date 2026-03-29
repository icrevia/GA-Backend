from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from core.config import settings
import os
import uuid
import logging
import time

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
    # OpenAPI docs are disabled in production (DEBUG=False)
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if settings.DEBUG else None,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─────────────────────────────────────────────
# Global Exception Handler
# Prevents leaking Python tracebacks to the client
# ─────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"rid={request_id} global_error={str(exc)}", exc_info=True)
    return Response(
        content='{"detail": "An internal server error occurred. Please contact support if this persists."}',
        status_code=500,
        media_type="application/json"
    )


# ─────────────────────────────────────────────
# Security headers middleware
# Adds OWASP-recommended headers to every response
# ─────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]         = "geolocation=(), camera=(), microphone=()"
        if not settings.DEBUG:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Don't cache API responses
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response


# ─────────────────────────────────────────────
# Request ID & timing middleware
# ─────────────────────────────────────────────
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            f"rid={request_id} method={request.method} path={request.url.path} "
            f"status={response.status_code} duration={duration_ms:.1f}ms"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# ─────────────────────────────────────────────
# Static Files
# ─────────────────────────────────────────────
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────────
# CORS — explicit origins only, never wildcard
# ─────────────────────────────────────────────
def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


ALLOWED_ORIGINS = []
for raw_origin in settings.ALLOWED_ORIGINS.split(","):
    normalized = _normalize_origin(raw_origin)
    if normalized and normalized not in ALLOWED_ORIGINS:
        ALLOWED_ORIGINS.append(normalized)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
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

    # One-time safe column migrations for new fields added to existing production DB.
    # Uses IF NOT EXISTS so it's a no-op after first run.
    from core.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER DEFAULT 0"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20)"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(255) UNIQUE"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_id INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_order_id VARCHAR(255)"
            ))
            conn.execute(text(
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_payment_id VARCHAR(255)"
            ))
            conn.execute(text(
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_signature VARCHAR(512)"
            ))
            conn.execute(text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS requires_admin BOOLEAN DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_by_admin_id INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_at TIMESTAMP"
            ))
            conn.execute(text(
                "UPDATE chat_sessions SET requires_admin = FALSE WHERE requires_admin IS NULL"
            ))
            conn.commit()
            logger.info("DB migration: referral_code and referred_by_id columns ensured")
        except Exception as e:
            logger.warning(f"DB migration skipped (non-critical): {e}")


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
            "maintenance_mode":   maintenance_mode,
            "status":             "maintenance" if maintenance_mode else "online",
            "message":            config_map.get(
                "maintenance_message",
                "Fine-tuning the gears. We'll be back in just a blink!"
            ),
            "until":              config_map.get("maintenance_until", ""),
            "latest_version_code": int(config_map.get("latest_version_code", "1")),
            "latest_version_name": config_map.get("latest_version_name", "1.0"),
            "update_url":         config_map.get("update_url", ""),
            "force_update":       config_map.get("force_update", "false").lower() == "true",
            "update_message":     config_map.get(
                "update_message",
                "A new version of ZexPlay is available! Upgrade now for the latest features."
            ),
            "payu_merchant_vpa":  settings.PAYU_MERCHANT_VPA,
            "support_email":      "support@zexplay.com" # Example, could be from settings
        }
    finally:
        db.close()
