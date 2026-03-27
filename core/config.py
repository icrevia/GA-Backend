from pydantic_settings import BaseSettings
from pydantic import model_validator
import sys
import logging

logger = logging.getLogger("zexplay.config")


class Settings(BaseSettings):
    PROJECT_NAME: str = "ZexPlay"
    API_V1_STR:   str = "/api/v1"
    DEBUG:        bool = False

    # ── Security ──────────────────────────────────────────────────────────────
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = "CHANGE_ME"
    ALGORITHM:  str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # ── App URL ───────────────────────────────────────────────────────────────
    # Set this in Railway environment variables.
    # Railway also provides RAILWAY_PUBLIC_DOMAIN automatically — we fall back to it.
    APP_URL:             str = ""
    RAILWAY_PUBLIC_DOMAIN: str = ""  # Injected automatically by Railway

    # ── CORS ──────────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = ""

    # ── PayU ──────────────────────────────────────────────────────────────────
    PAYU_MERCHANT_KEY:  str = "TEST_KEY"
    PAYU_MERCHANT_SALT: str = "TEST_SALT"
    PAYU_BASE_URL:      str = "https://secure.payu.in"
    PAYU_MERCHANT_VPA:  str = "zexplay@ybl"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore unknown Railway-injected env vars

    @model_validator(mode="after")
    def resolve_and_validate(self) -> "Settings":
        # ── Resolve APP_URL from Railway's auto-injected domain if not set directly ──
        if not self.APP_URL and self.RAILWAY_PUBLIC_DOMAIN:
            object.__setattr__(
                self, "APP_URL",
                f"https://{self.RAILWAY_PUBLIC_DOMAIN}"
            )

        # ── ALLOWED_ORIGINS defaults to APP_URL if not overridden ─────────────
        if not self.ALLOWED_ORIGINS and self.APP_URL:
            object.__setattr__(self, "ALLOWED_ORIGINS", self.APP_URL)

        # ── HARD FAIL: Only crash if SECRET_KEY is still the placeholder ──────
        # Everything else gets a warning — we don't want to take prod offline.
        placeholder_keys = {
            "CHANGE_ME",
            "",
            "CHANGE_ME_GENERATE_WITH_secrets_token_hex_32",
        }
        if self.SECRET_KEY in placeholder_keys:
            print(
                "[CONFIG FATAL] SECRET_KEY is not set or is using the default placeholder.\n"
                "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\"",
                file=sys.stderr
            )
            raise ValueError("SECRET_KEY must be set to a strong random value in production.")

        # ── SOFT WARNINGS: Log but don't crash ────────────────────────────────
        if not self.APP_URL:
            print(
                "[CONFIG WARNING] APP_URL is not set. PayU payment callbacks (SURL/FURL) "
                "will not work correctly. Set APP_URL in Railway environment variables.",
                file=sys.stderr
            )

        if not self.DATABASE_URL:
            print(
                "[CONFIG WARNING] DATABASE_URL is not set. The app will fail on DB operations.",
                file=sys.stderr
            )

        if not self.ALLOWED_ORIGINS:
            # Last resort: allow all — not ideal, but keeps the app alive
            object.__setattr__(self, "ALLOWED_ORIGINS", "*")
            print(
                "[CONFIG WARNING] ALLOWED_ORIGINS not set. Defaulting to '*' (insecure). "
                "Set ALLOWED_ORIGINS in Railway environment variables.",
                file=sys.stderr
            )

        return self


settings = Settings()
