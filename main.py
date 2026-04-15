from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from core.config import settings
from services.login_security import extract_client_ip, is_ip_blocked
from contextlib import asynccontextmanager, suppress
import asyncio
import os
import uuid
import logging
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("GamerzAdda")

from api.router import api_router
from core.database import engine, Base
from models import user, tournament, wallet, support, withdraw_upi_account, promo, banner, restriction, otp_phone_lock, user_activity_lock

SYSTEM_STATUS_CACHE_TTL_SECONDS = 15.0
_system_status_cache: dict[str, object] = {
    "expires_at": 0.0,
    "value": None,
}

# ─────────────────────────────────────────────
# Lifespan context manager (Modern approach)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    logger.info("GamerzAdda API starting up (Lifespan)...")
    support_media_cleanup_task: asyncio.Task | None = None
    bonus_expiry_task: asyncio.Task | None = None

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
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS deposit_balance NUMERIC(12,2) DEFAULT 0.00",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS winning_balance NUMERIC(12,2) DEFAULT 0.00",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_balance NUMERIC(12,2) DEFAULT 0.00",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(64)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_device VARCHAR(160)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_spin_limit INTEGER DEFAULT 1",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_spin_used INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_spin_cycle_key VARCHAR(16)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(255) UNIQUE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_id INTEGER",
                # FCM push notification token
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(512)",

                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_order_id VARCHAR(255)",
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_payment_id VARCHAR(255)",
                "ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS gateway_signature VARCHAR(512)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS requires_admin BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_by_admin_id INTEGER",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS attended_at TIMESTAMP",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS blocked_by_admin_id INTEGER",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS ended_by_user_id INTEGER",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS ended_by_role VARCHAR(16)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS issue_type VARCHAR(120)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS issue_ack_sent BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS is_user_blocked BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_type VARCHAR(16)",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_url TEXT",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_path TEXT",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_mime_type VARCHAR(120)",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_size_bytes INTEGER",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_expires_at TIMESTAMP",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS slot_no INTEGER",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS account_level INTEGER",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_members TEXT",
                # Team-based join system
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_name TEXT",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS team_join_code TEXT",
                "ALTER TABLE tournament_participants ADD COLUMN IF NOT EXISTS is_team_captain BOOLEAN DEFAULT FALSE",
                "CREATE INDEX IF NOT EXISTS ix_tp_team_join_code ON tournament_participants (team_join_code) WHERE team_join_code IS NOT NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tournament_participant_slot_idx ON tournament_participants (tournament_id, slot_no) WHERE slot_no IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS ix_wallet_tx_type_status_created_at ON wallet_transactions (transaction_type, status, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_wallet_tx_created_at ON wallet_transactions (created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_tournaments_status_match_time ON tournaments (status, match_time)",
                "CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_created_at ON chat_sessions (user_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_is_read ON chat_messages (session_id, is_read)",
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_media_expires_at ON chat_messages (media_expires_at)",
            ]
            for query in queries:
                await conn.execute(text(query))

            # Backfill for legacy accounts where only wallet_balance existed.
            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET deposit_balance = COALESCE(wallet_balance, 0.00)
                    WHERE COALESCE(wallet_balance, 0.00) > 0.00
                      AND COALESCE(deposit_balance, 0.00) = 0.00
                      AND COALESCE(winning_balance, 0.00) = 0.00
                      AND COALESCE(bonus_balance, 0.00) = 0.00
                    """
                )
            )

            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET daily_spin_limit = COALESCE(daily_spin_limit, 1),
                        daily_spin_used = COALESCE(daily_spin_used, 0)
                    """
                )
            )

            # Best-effort cleanup: these legacy fields are no longer stored in users table.
            # Keep failures non-fatal for engines that do not support DROP COLUMN IF EXISTS.
            cleanup_queries = [
                "DROP INDEX IF EXISTS ix_users_firebase_uid",
                "ALTER TABLE users DROP COLUMN IF EXISTS firebase_uid",
                "ALTER TABLE users DROP COLUMN IF EXISTS upi_id",
                "ALTER TABLE users DROP COLUMN IF EXISTS bgmi_id",
                "ALTER TABLE users DROP COLUMN IF EXISTS valorant_id",
            ]
            for query in cleanup_queries:
                try:
                    await conn.execute(text(query))
                except Exception as cleanup_error:
                    logger.info("Skipped optional users cleanup query '%s': %s", query, cleanup_error)

            await conn.commit()
            logger.info("DB sync & migration successful")
        except Exception as e:
            logger.warning(f"DB migration partial failure: {e}")

    from services.support_media import ensure_support_media_storage_dir, support_media_cleanup_worker

    try:
        ensure_support_media_storage_dir()
    except Exception as media_dir_error:
        logger.warning("Support media storage dir init failed: %s", media_dir_error)

    support_media_cleanup_task = asyncio.create_task(support_media_cleanup_worker())
    logger.info("Support media cleanup worker started")

    # ── Startup push notification to all users ───────────────────
    async def _send_startup_notification():
        try:
            from services.push_notifications import send_push_to_many
            from core.database import SessionLocal
            from models.user import User as UserModel
            async with SessionLocal() as db:
                from sqlalchemy import select
                result = await db.execute(
                    select(UserModel.fcm_token).where(UserModel.fcm_token.isnot(None))
                )
                tokens = [row[0] for row in result.fetchall() if row[0]]
            if tokens:
                import threading
                threading.Thread(
                    target=send_push_to_many,
                    args=(
                        tokens,
                        "🚀 GamerzAdda is Live!",
                        "Server is active — App is running smooth! Check out new tournaments 🎮",
                    ),
                    daemon=True,
                ).start()
                logger.info("Startup notification sent to %d devices", len(tokens))
        except Exception as notif_err:
            logger.warning("Startup notification failed: %s", notif_err)

    asyncio.create_task(_send_startup_notification())
    # ─────────────────────────────────────────────────────────────

    # ── Automated Bonus Expiry Worker (runs every 6 hours) ───────
    BONUS_EXPIRY_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours

    async def _bonus_expiry_worker():
        await asyncio.sleep(30)  # initial delay to let DB settle
        while True:
            try:
                from core.database import SyncSessionLocal
                from services.bonus_expiry import run_bonus_expiry_cycle

                db = SyncSessionLocal()
                try:
                    result = run_bonus_expiry_cycle(db)
                    if result["expired"] > 0 or result["reminders"] > 0:
                        logger.info(
                            "Bonus expiry worker: expired=%d, reminders=%d",
                            result["expired"], result["reminders"],
                        )
                finally:
                    db.close()
            except Exception as expiry_err:
                logger.error("Bonus expiry worker error: %s", expiry_err)

            await asyncio.sleep(BONUS_EXPIRY_INTERVAL_SECONDS)

    bonus_expiry_task = asyncio.create_task(_bonus_expiry_worker())
    logger.info("Bonus expiry background worker started (interval: 6h)")
    # ─────────────────────────────────────────────────────────────

    try:
        yield
    finally:
        if support_media_cleanup_task is not None:
            support_media_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await support_media_cleanup_task
        if bonus_expiry_task is not None:
            bonus_expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await bonus_expiry_task
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
        
        # Add a flag to track if this is a heavy request
        response = await call_next(request)
        
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-MS"] = f"{duration_ms:.2f}"
        
        logger.info(f"rid={request_id} {request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms")
        return response

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)

if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

support_media_dir = Path(settings.SUPPORT_MEDIA_STORAGE_DIR).expanduser()
if not support_media_dir.is_absolute():
    support_media_dir = (Path.cwd() / support_media_dir).resolve()

try:
    support_media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/support", StaticFiles(directory=str(support_media_dir)), name="support_media")
    logger.info("Support media static mount enabled at /support -> %s", support_media_dir)
except Exception as support_mount_error:
    logger.warning("Support media static mount failed: %s", support_mount_error)

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
    now = time.monotonic()
    cached_until = float(_system_status_cache.get("expires_at", 0.0) or 0.0)
    cached_payload = _system_status_cache.get("value")
    if cached_payload is not None and now < cached_until:
        return cached_payload

    from core.database import SessionLocal
    from models.config import SystemConfig
    from sqlalchemy import select
    async with SessionLocal() as db:
        try:
            result = await db.execute(select(SystemConfig))
            configs = result.scalars().all()
            config_map = {c.config_key: c.config_value for c in configs}
            payload = {
                "maintenance_mode": config_map.get("maintenance_mode", "false").lower() == "true",
                "status": "online",
                "latest_version_code": int(config_map.get("latest_version_code", "1")),
                "support_email": "support@gamerzadda.in"
            }
            _system_status_cache["value"] = payload
            _system_status_cache["expires_at"] = now + SYSTEM_STATUS_CACHE_TTL_SECONDS
            return payload
        except Exception as e:
            return {"status": "degraded", "error": str(e)}
