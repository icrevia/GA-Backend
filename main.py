from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
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
from models import user, tournament, wallet, support, withdraw_upi_account, promo, banner, restriction, otp_phone_lock, user_activity_lock, admin_access_session

SYSTEM_STATUS_CACHE_TTL_SECONDS = 15.0
_system_status_cache: dict[str, object] = {
    "expires_at": 0.0,
    "value": None,
}


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _status_payload_from_config(config_map: dict[str, str]) -> dict[str, object]:
    maintenance_mode = _as_bool(config_map.get("maintenance_mode"), False)
    return {
        "maintenance_mode": maintenance_mode,
        "status": "maintenance" if maintenance_mode else "online",
        "message": (config_map.get("maintenance_message") or "").strip(),
        "until": (config_map.get("maintenance_until") or "").strip(),
        "latest_version_code": _as_int(config_map.get("latest_version_code"), 1),
        "latest_version_name": (config_map.get("latest_version_name") or "1.0").strip() or "1.0",
        "update_url": (config_map.get("update_url") or "").strip(),
        "force_update": _as_bool(config_map.get("force_update"), False),
        "update_message": (config_map.get("update_message") or "").strip(),
        "support_email": (config_map.get("support_email") or "gamerzaddahelp@gmail.com").strip(),
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
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_bonus_used NUMERIC(12,2) DEFAULT 0.00",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_bonus_cycle_key VARCHAR(16)",
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
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_cleared_at TIMESTAMP",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS ended_by_role VARCHAR(16)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS issue_type VARCHAR(120)",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS issue_ack_sent BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS is_user_blocked BOOLEAN DEFAULT FALSE",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_type VARCHAR(16)",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_url TEXT",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_path TEXT",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_mime_type VARCHAR(120)",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_size_bytes INTEGER",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_duration_seconds DOUBLE PRECISION",
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS media_expires_at TIMESTAMP",
                "ALTER TABLE IF EXISTS chat_messages DROP CONSTRAINT IF EXISTS ck_chat_messages_media_type",
                "ALTER TABLE IF EXISTS chat_messages ADD CONSTRAINT ck_chat_messages_media_type CHECK ((media_type IS NULL) OR (media_type IN ('photo', 'audio', 'video')))",
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
                # Prize distribution per rank
                "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS prize_distribution JSONB",
                # Map name
                "ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS map_name VARCHAR(100)",
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
                        daily_spin_used = COALESCE(daily_spin_used, 0),
                        daily_bonus_used = COALESCE(daily_bonus_used, 0.00)
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
    from services.ledger_bot import register_ledger_bot_webhook

    try:
        ensure_support_media_storage_dir()
    except Exception as media_dir_error:
        logger.warning("Support media storage dir init failed: %s", media_dir_error)

    support_media_cleanup_task = asyncio.create_task(support_media_cleanup_worker())
    logger.info("Support media cleanup worker started")

    # ── Startup push notification to all users ───────────────────
    async def _send_startup_notification():
        try:
            from services.push_notifications import send_push_to_many_detailed
            from core.database import SessionLocal
            from models.user import User as UserModel
            async with SessionLocal() as db:
                from sqlalchemy import select, update
                result = await db.execute(
                    select(UserModel.fcm_token).where(UserModel.fcm_token.isnot(None))
                )
                tokens = [row[0] for row in result.fetchall() if row[0]]

            if tokens:
                push_result = await asyncio.to_thread(
                    send_push_to_many_detailed,
                    tokens,
                    "🚀 GamerzAdda is Live! -- Firebase",
                    "Server is active — Check out new tournaments 🎮 -- Firebase",
                )

                sent = int(push_result.get("success_count", 0))
                invalid_tokens = [token for token in push_result.get("invalid_tokens", []) if token]
                cleared = 0

                if invalid_tokens:
                    async with SessionLocal() as db:
                        update_result = await db.execute(
                            update(UserModel)
                            .where(UserModel.fcm_token.in_(invalid_tokens))
                            .values(fcm_token=None)
                        )
                        await db.commit()
                        cleared = int(update_result.rowcount or 0)

                logger.info(
                    "Startup notification sent: %d/%d via Push (%d stale token(s) cleared)",
                    sent,
                    len(tokens),
                    cleared,
                )
        except Exception as notif_err:
            logger.warning("Startup notification failed: %s", notif_err)

    asyncio.create_task(_send_startup_notification())
    # ─────────────────────────────────────────────────────────────

    asyncio.create_task(asyncio.to_thread(register_ledger_bot_webhook))

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
# Ensure sub-directories exist for file uploads
os.makedirs("static/profile_pics", exist_ok=True)
os.makedirs("static/banners", exist_ok=True)
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

BROWSER_UA_HINTS = (
    "mozilla/",
    "chrome/",
    "safari/",
    "firefox/",
    "edg/",
    "opera/",
)


def _is_browser_navigation(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    user_agent = request.headers.get("user-agent", "").lower()
    sec_fetch_mode = request.headers.get("sec-fetch-mode", "").lower()
    sec_fetch_dest = request.headers.get("sec-fetch-dest", "").lower()

    accepts_document = (
        "text/html" in accept
        or "application/xhtml+xml" in accept
        or (sec_fetch_mode == "navigate" and sec_fetch_dest == "document")
    )
    has_browser_ua = any(hint in user_agent for hint in BROWSER_UA_HINTS)
    return accepts_document and has_browser_ua


@app.get("/")
def root(request: Request):
    json_payload = {"message": "GamerzAdda API — Online", "version": "2.0"}

    if not _is_browser_navigation(request):
        return JSONResponse(content=json_payload)

    html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>GamerzAdda API Gateway</title>
            <style>
                :root {
                    --bg-a: #0D0E12;
                    --bg-b: #1A1C23;
                    --panel: rgba(26, 28, 35, 0.8);
                    --line: rgba(239, 68, 68, 0.3);
                    --text: #F3F4F6;
                    --muted: #9CA3AF;
                    --accent: #EF4444;
                }

                body {
                    margin: 0;
                    min-height: 100vh;
                    display: grid;
                    place-items: center;
                    font-family: 'Inter', sans-serif;
                    color: var(--text);
                    background: linear-gradient(135deg, var(--bg-a), var(--bg-b));
                }

                .wrap {
                    width: min(500px, 90vw);
                    border: 1px solid var(--line);
                    background: var(--panel);
                    border-radius: 20px;
                    padding: 2.5rem;
                    text-align: center;
                    backdrop-filter: blur(10px);
                    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
                }

                h1 { margin: 1rem 0; font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
                p { color: var(--muted); line-height: 1.6; }
                .status { 
                    display: inline-flex; align-items: center; gap: 8px; 
                    background: rgba(16, 185, 129, 0.1); color: #10B981;
                    padding: 6px 12px; border-radius: 999px; font-size: 0.85rem; font-weight: 700;
                }
                .dot { width: 8px; height: 8px; background: currentColor; border-radius: 50%; }
            </style>
        </head>
        <body>
            <main class="wrap">
                <div class="status"><span class="dot"></span> API SYSTEMS ONLINE</div>
                <h1>GamerzAdda Gateway</h1>
                <p>
                    You have reached the official GamerzAdda backend. 
                    This system is protected and monitored. Unauthorized access is prohibited.
                </p>
                <div style="margin-top: 2rem; font-size: 0.8rem; color: var(--muted);">
                    &copy; 2026 GamerzAdda &bull; Powered by Zxtni Studio
                </div>
            </main>
        </body>
        </html>
    """
    return HTMLResponse(content=html)

@app.get("/api/v1/status")
async def get_system_status(request: Request):
    if _is_browser_navigation(request):
        return root(request)

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
            payload = _status_payload_from_config(config_map)
            _system_status_cache["value"] = payload
            _system_status_cache["expires_at"] = now + SYSTEM_STATUS_CACHE_TTL_SECONDS
            return payload
        except Exception as e:
            return {"status": "degraded", "error": str(e)}
