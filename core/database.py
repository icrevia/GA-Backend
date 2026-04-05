from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker, declarative_base
from core.config import settings
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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

# Correct Async Engine for asyncpg driver
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True
)

sync_engine = create_engine(
    _to_sync_database_url(settings.DATABASE_URL),
    echo=False,
    future=True,
    pool_pre_ping=True,
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
