from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker, declarative_base
from core.config import settings
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import ssl
import os
import json


def _to_sync_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if "+asyncpg" in database_url:
        database_url = database_url.replace("+asyncpg", "+psycopg2")

    parsed = urlsplit(database_url)
    if not parsed.query:
        return database_url

    # psycopg2 rejects some asyncpg-only query options.
    asyncpg_only_keys = {
        "prepared_statement_cache_size",
        "statement_cache_size",
        "command_timeout",
        "server_settings",
    }
    normalized_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in asyncpg_only_keys:
            continue
        if lowered == "ssl":
            normalized_params.append(("sslmode", value or "require"))
            continue
        normalized_params.append((key, value))

    rebuilt = parsed._replace(query=urlencode(normalized_params, doseq=True))
    return urlunsplit(rebuilt)


def _to_async_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif database_url.startswith("postgresql://") and "+asyncpg" not in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlsplit(database_url)
    if not parsed.query:
        return database_url

    normalized_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered == "sslmode":
            mode = (value or "").strip().lower()
            if mode in {"disable", "false", "0"}:
                normalized_params.append(("ssl", "false"))
            else:
                normalized_params.append(("ssl", "true"))
            continue
        normalized_params.append((key, value))

    rebuilt = parsed._replace(query=urlencode(normalized_params, doseq=True))
    return urlunsplit(rebuilt)


def _strip_sslmode(database_url: str) -> tuple[str, str | None]:
    parsed = urlsplit(database_url)
    if not parsed.query:
        return database_url, None

    sslmode_value: str | None = None
    remaining_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "sslmode":
            sslmode_value = value
            continue
        remaining_params.append((key, value))

    rebuilt = parsed._replace(query=urlencode(remaining_params, doseq=True))
    return urlunsplit(rebuilt), sslmode_value


def _pool_kwargs_for_url(database_url: str) -> dict:
    lowered = (database_url or "").lower()
    if lowered.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT_SECONDS,
        "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
    }


raw_async_url, sslmode = _strip_sslmode(settings.DATABASE_URL)
async_url = _to_async_database_url(raw_async_url)

async_connect_args: dict[str, object] = {}
if sslmode:
    normalized_sslmode = sslmode.strip().lower()
    if normalized_sslmode in {"disable", "false", "0"}:
        async_connect_args["ssl"] = False
    else:
        ssl_insecure = os.getenv("DB_SSL_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}
        if ssl_insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            async_connect_args["ssl"] = ctx
        else:
            async_connect_args["ssl"] = True

sync_url = _to_sync_database_url(settings.DATABASE_URL)

# Correct Async Engine for asyncpg driver
engine = create_async_engine(
    async_url,
    echo=False,
    future=True,
    connect_args=async_connect_args,
    prepared_statement_cache_size=0,
    **_pool_kwargs_for_url(async_url)
)

sync_engine = create_engine(
    sync_url,
    echo=False,
    future=True,
    **_pool_kwargs_for_url(sync_url),
)

# Async Session Factory
SessionLocal = sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()

# Async Dependency for FastAPI
async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


def get_db_sync():
    session: Session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
