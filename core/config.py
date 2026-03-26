from pydantic_settings import BaseSettings
from pydantic import model_validator
import sys


class Settings(BaseSettings):
    PROJECT_NAME: str = "ZexPlay"
    API_V1_STR:   str = "/api/v1"
    DEBUG:        bool = False  # Set to True only in local dev via .env

    # ── Security ──────────────────────────────────────────────────────────────
    # REQUIRED in production. Generate:
    #   python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = "CHANGE_ME"
    ALGORITHM:  str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # ── App URL ───────────────────────────────────────────────────────────────
    # REQUIRED — must be set in Railway env vars. No trailing slash.
    APP_URL: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins. Must be set in env for prod.
    ALLOWED_ORIGINS: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    # REQUIRED — full Postgres connection string
    DATABASE_URL: str = ""

    # ── PayU ──────────────────────────────────────────────────────────────────
    PAYU_MERCHANT_KEY:  str = "TEST_KEY"
    PAYU_MERCHANT_SALT: str = "TEST_SALT"
    PAYU_BASE_URL:      str = "https://test.payu.in"
    PAYU_MERCHANT_VPA:  str = "zexplay@ybl"

    class Config:
        env_file = ".env"
        case_sensitive = True

    @model_validator(mode="after")
    def guard_required_secrets(self) -> "Settings":
        """
        Refuse to start if critical secrets are missing or still set to placeholder values.
        This prevents deploying with insecure defaults.
        """
        errors = []

        if self.SECRET_KEY in ("CHANGE_ME", "", "CHANGE_ME_GENERATE_WITH_secrets_token_hex_32"):
            errors.append(
                "SECRET_KEY is not set. "
                "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        if not self.APP_URL:
            errors.append("APP_URL is required (e.g. https://your-domain.com)")

        if not self.DATABASE_URL:
            errors.append("DATABASE_URL is required")

        if not self.ALLOWED_ORIGINS:
            # Default to APP_URL if not explicitly set
            object.__setattr__(self, "ALLOWED_ORIGINS", self.APP_URL)

        if errors:
            for e in errors:
                print(f"[CONFIG ERROR] {e}", file=sys.stderr)
            raise ValueError(
                f"Missing required environment variables ({len(errors)} error(s)). "
                "Set them in your .env file or deployment environment."
            )

        return self


settings = Settings()
