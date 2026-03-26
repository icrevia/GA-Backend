from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    PROJECT_NAME: str = "ZexPlay"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = False  # Set to True only in local dev via .env

    # ── Security ──────────────────────────────────────────────────────────────
    # REQUIRED: set a long random secret in .env — never use the default in prod
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = "CHANGE_ME_GENERATE_WITH_secrets_token_hex_32"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # ── App URL ───────────────────────────────────────────────────────────────
    APP_URL: str = "https://web-production-051ba.up.railway.app"

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins. Set in .env for prod.
    ALLOWED_ORIGINS: str = "https://web-production-051ba.up.railway.app"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/zexplay_db"

    # ── PayU ──────────────────────────────────────────────────────────────────
    PAYU_MERCHANT_KEY: str = "TEST_MERCHANT_KEY"
    PAYU_MERCHANT_SALT: str = "TEST_MERCHANT_SALT"
    PAYU_BASE_URL: str = "https://test.payu.in"
    PAYU_MERCHANT_VPA: str = "zexplay@ybl"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
