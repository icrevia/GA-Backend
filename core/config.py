from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from pydantic import model_validator
import sys
import logging
import os

logger = logging.getLogger("GamerzAdda.config")


class Settings(BaseSettings):
    PROJECT_NAME: str = "GamerzAdda"
    API_V1_STR:   str = "/api/v1"
    ENVIRONMENT:  str = "development"
    DEBUG:        bool = False

    # ── Security ──────────────────────────────────────────────────────────────
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = ""
    ALGORITHM:  str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080 # 7 days (60 * 24 * 7)

    # ── Login security controls ──────────────────────────────────────────────
    ENABLE_LOGIN_IP_BLOCK: bool = True
    LOGIN_FAILURE_WINDOW_SECONDS: int = 900
    LOGIN_FAILURE_BLOCK_THRESHOLD: int = 8
    LOGIN_FAILURE_BLOCK_SECONDS: int = 1800

    # ── Admin panel login source (Railway env) ─────────────────────────────
    # Admin-web login identifier and OTP phone must come from env vars,
    # not database identifier matching.
    ADMIN_LOGIN_IDENTIFIER: str = ""
    ADMIN_LOGIN_PHONE: str = ""
    ADMIN_LOGIN_TELEGRAM_CHAT_ID: str = ""

    # ── Telegram security alerts ─────────────────────────────────────────────
    SECURITY_ALERTS_ENABLED: bool = False
    SECURITY_ALERT_ON_SUCCESS_LOGIN: bool = True
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALERT_CHAT_ID: str = ""
    CHAT_NOTI: str = ""
    SECURITY_ALERT_TIMEOUT_SECONDS: float = 3.0
    ENABLE_IP_GEO_LOOKUP: bool = True
    IP_GEO_LOOKUP_TIMEOUT_SECONDS: float = 2.0
    ADMIN_BLOCK_LOGIN_ON_GEO_DENIED: bool = True

    # ── Ledger bot (withdraw approvals via Telegram) ────────────────────────
    LEDGER_BOT: str = ""
    LEDGER_ADMINS: str = ""
    LEDGER_WEBHOOK_SECRET: str = ""

    # ── Developer page OTP gate ─────────────────────────────────────────────
    DEVELOPER_OTP_ENABLED: bool = True
    DEVELOPER_OTP_LENGTH: int = 6
    DEVELOPER_OTP_TTL_SECONDS: int = 300
    DEVELOPER_OTP_MAX_VERIFY_ATTEMPTS: int = 5
    DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS: int = 30
    DEVELOPER_OTP_SESSION_TTL_SECONDS: int = 1800
    DEVELOPER_OTP_TELEGRAM_CHAT_ID: str = ""

    # ── App URL ───────────────────────────────────────────────────────────────
    # In production, set this explicitly from Railway Variables.
    APP_URL: str = ""
    SUPPORT_WHATSAPP_NUMBER: str = "917632932544"

    # ── Support media storage ────────────────────────────────────────────────
    SUPPORT_MEDIA_STORAGE_DIR: str = "static/support_media"
    SUPPORT_MEDIA_PUBLIC_PREFIX: str = "/support"
    SUPPORT_MEDIA_PUBLIC_BASE_URL: str = ""
    SUPPORT_MEDIA_RETENTION_HOURS: int = 24
    SUPPORT_MEDIA_CLEANUP_INTERVAL_MINUTES: int = 15
    SUPPORT_MEDIA_PHOTO_MAX_MB: int = 2
    SUPPORT_MEDIA_AUDIO_MAX_MB: int = 20
    SUPPORT_MEDIA_VIDEO_MAX_MB: int = 50

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of origins: "https://admin.GamerzAdda.com, http://localhost:3000"
    ALLOWED_ORIGINS: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT_SECONDS: int = 30
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Pay0.shop ─────────────────────────────────────────────────────────────
    PAY0_MERCHANT_KEY: str = ""

    # ── Message Central OTP ───────────────────────────────────────────────────
    MC_CUSTOMER_ID: str = ""
    MC_AUTH_TOKEN:  str = ""

    # ── Email OTP (login only) ───────────────────────────────────────────────
    EMAIL_OTP_ENABLED: bool = False
    EMAIL_OTP_LENGTH: int = 4
    EMAIL_OTP_TTL_SECONDS: int = 300
    EMAIL_OTP_MAX_VERIFY_ATTEMPTS: int = 5

    EMAIL_SMTP_HOST: str = ""
    EMAIL_SMTP_PORT: int = 587
    EMAIL_SMTP_USERNAME: str = ""
    EMAIL_SMTP_PASSWORD: str = ""
    EMAIL_SMTP_STARTTLS: bool = True
    EMAIL_SMTP_USE_SSL: bool = False
    EMAIL_SMTP_TIMEOUT_SECONDS: float = 15.0
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "GamerzAdda Security"

    # ── VPS Remote Storage (SFTP) ───────────────────────────────────────────
    VPS_STORAGE_ENABLED: bool = False
    VPS_HOST: str = ""
    VPS_PORT: int = 22
    VPS_USERNAME: str = ""
    VPS_PASSWORD: str = ""
    VPS_PRIVATE_KEY: str = ""
    VPS_REMOTE_PATH: str = "/var/www/html/static"
    VPS_PUBLIC_BASE_URL: str = "" # e.g. https://assets.gamerzadda.in
    
    # ── VPS API Storage (HTTP Bypass) ──────────────────────────────────────
    VPS_API_UPLOAD_URL: str = "" # e.g. https://gamerzadda.in/upload_handler.php
    VPS_API_SECRET: str = "GamerzAdda_Secret_2026"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore unknown Railway-injected env vars

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # In production, read only process env vars (Railway Variables section).
        # Local development can still use .env for convenience.
        env_name = os.getenv("ENVIRONMENT", "development").lower()
        if env_name in {"production", "prod"}:
            return (init_settings, env_settings, file_secret_settings)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    @model_validator(mode="after")
    def resolve_and_validate(self) -> "Settings":
        env_name = self.ENVIRONMENT.lower()
        is_production = env_name in {"production", "prod"}

        # Local-only defaults to reduce setup friction.
        pass

        # ── HARD FAIL: Only crash if SECRET_KEY is still the placeholder ──────
        # Everything else gets a warning — we don't want to take prod offline.
        placeholder_keys = {
            "CHANGE_ME",
            "",
            "CHANGE_ME_GENERATE_WITH_secrets_token_hex_32",
            "GamerzAdda_Super_Secure_JWT_Key_2026_@",
            "your-super-secret-key-change-this",
        }
        if self.SECRET_KEY in placeholder_keys or (is_production and len(self.SECRET_KEY) < 32):
            print(
                "[CONFIG FATAL] SECRET_KEY is missing/weak.\n"
                "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\"",
                file=sys.stderr
            )
            raise ValueError("SECRET_KEY must be set to a strong random value in production.")

        if is_production:
            required_fields = {
                "APP_URL": self.APP_URL,
                "ALLOWED_ORIGINS": self.ALLOWED_ORIGINS,
                "DATABASE_URL": self.DATABASE_URL,
            }
            missing = [key for key, value in required_fields.items() if not value]
            if missing:
                raise ValueError(
                    "Missing required production environment variables: " + ", ".join(missing)
                )

        # ── SOFT WARNINGS: Log but don't crash ────────────────────────────────
        if not self.APP_URL:
            print(
                "[CONFIG WARNING] APP_URL is not set. Payment callbacks "
                "will not work correctly. Set APP_URL in Railway environment variables.",
                file=sys.stderr
            )

        if not self.DATABASE_URL:
            print(
                "[CONFIG WARNING] DATABASE_URL is not set. The app will fail on DB operations.",
                file=sys.stderr
            )

        if self.DB_POOL_SIZE < 1:
            print(
                "[CONFIG WARNING] DB_POOL_SIZE must be >= 1. Falling back to 1.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DB_POOL_SIZE", 1)

        if self.DB_MAX_OVERFLOW < 0:
            print(
                "[CONFIG WARNING] DB_MAX_OVERFLOW cannot be negative. Falling back to 0.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DB_MAX_OVERFLOW", 0)

        if self.DB_POOL_TIMEOUT_SECONDS <= 0:
            print(
                "[CONFIG WARNING] DB_POOL_TIMEOUT_SECONDS must be positive. Falling back to 30.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DB_POOL_TIMEOUT_SECONDS", 30)

        if self.DB_POOL_RECYCLE_SECONDS <= 0:
            print(
                "[CONFIG WARNING] DB_POOL_RECYCLE_SECONDS must be positive. Falling back to 1800.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DB_POOL_RECYCLE_SECONDS", 1800)

        if not self.ALLOWED_ORIGINS:
            if self.DEBUG:
                object.__setattr__(self, "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
                print(
                    "[CONFIG WARNING] ALLOWED_ORIGINS not set. Using local debug defaults.",
                    file=sys.stderr
                )
            else:
                raise ValueError(
                    "ALLOWED_ORIGINS must be explicitly configured in production. "
                    "Refusing to start with implicit wildcard behavior."
                )
        
        # ── Pay0 Soft Warning ─────────────────────────────────────────────
        if not self.PAY0_MERCHANT_KEY:
            print(
                "[CONFIG WARNING] PAY0_MERCHANT_KEY is not set. "
                "Payments via UPI will fail.",
                file=sys.stderr
            )

        if self.EMAIL_OTP_ENABLED:
            print(
                "[CONFIG WARNING] EMAIL_OTP_ENABLED is deprecated and forced off. OTP delivery is phone-only.",
                file=sys.stderr,
            )
            object.__setattr__(self, "EMAIL_OTP_ENABLED", False)

        if self.SECURITY_ALERTS_ENABLED and (not self.TELEGRAM_BOT_TOKEN or not self.TELEGRAM_ALERT_CHAT_ID):
            print(
                "[CONFIG WARNING] SECURITY_ALERTS_ENABLED is true but TELEGRAM_BOT_TOKEN/"
                "TELEGRAM_ALERT_CHAT_ID is missing. Disabling Telegram security alerts.",
                file=sys.stderr
            )
            object.__setattr__(self, "SECURITY_ALERTS_ENABLED", False)

        if self.LEDGER_BOT and not self.LEDGER_ADMINS:
            print(
                "[CONFIG WARNING] LEDGER_BOT is set but LEDGER_ADMINS is empty. "
                "Withdrawal alerts cannot be delivered to Telegram admins.",
                file=sys.stderr,
            )

        if self.LEDGER_ADMINS and not self.LEDGER_BOT:
            print(
                "[CONFIG WARNING] LEDGER_ADMINS is set but LEDGER_BOT is empty. "
                "Set LEDGER_BOT with a Telegram bot token.",
                file=sys.stderr,
            )

        if self.LEDGER_BOT and not self.LEDGER_WEBHOOK_SECRET:
            print(
                "[CONFIG WARNING] LEDGER_WEBHOOK_SECRET is empty. "
                "Telegram webhook calls will rely only on callback signature checks.",
                file=sys.stderr,
            )

        if not self.ADMIN_LOGIN_PHONE:
            print(
                "[CONFIG WARNING] ADMIN_LOGIN_PHONE is empty. Admin-web phone login will be blocked.",
                file=sys.stderr,
            )

        if self.ADMIN_LOGIN_PHONE and not self.TELEGRAM_BOT_TOKEN:
            print(
                "[CONFIG WARNING] Admin-web OTP via Telegram requires TELEGRAM_BOT_TOKEN.",
                file=sys.stderr,
            )

        if (
            self.ADMIN_LOGIN_PHONE
            and not (self.ADMIN_LOGIN_TELEGRAM_CHAT_ID or self.TELEGRAM_ALERT_CHAT_ID)
        ):
            print(
                "[CONFIG WARNING] Set ADMIN_LOGIN_TELEGRAM_CHAT_ID (or TELEGRAM_ALERT_CHAT_ID) "
                "to receive admin login OTP in Telegram.",
                file=sys.stderr,
            )

        if self.DEVELOPER_OTP_ENABLED and not self.TELEGRAM_BOT_TOKEN:
            print(
                "[CONFIG WARNING] Developer OTP is enabled but TELEGRAM_BOT_TOKEN is missing. "
                "Developer page OTP delivery will fail.",
                file=sys.stderr,
            )

        if (
            self.DEVELOPER_OTP_ENABLED
            and not (self.DEVELOPER_OTP_TELEGRAM_CHAT_ID or self.TELEGRAM_ALERT_CHAT_ID)
        ):
            print(
                "[CONFIG WARNING] Developer OTP is enabled but no target chat is configured. "
                "Set DEVELOPER_OTP_TELEGRAM_CHAT_ID (comma-separated allowed) or TELEGRAM_ALERT_CHAT_ID.",
                file=sys.stderr,
            )

        if self.IP_GEO_LOOKUP_TIMEOUT_SECONDS <= 0:
            print(
                "[CONFIG WARNING] IP_GEO_LOOKUP_TIMEOUT_SECONDS must be positive. Falling back to 2.0 seconds.",
                file=sys.stderr,
            )
            object.__setattr__(self, "IP_GEO_LOOKUP_TIMEOUT_SECONDS", 2.0)

        if self.SUPPORT_MEDIA_RETENTION_HOURS <= 0:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_RETENTION_HOURS must be positive. Falling back to 24.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_RETENTION_HOURS", 24)

        if self.SUPPORT_MEDIA_CLEANUP_INTERVAL_MINUTES <= 0:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_CLEANUP_INTERVAL_MINUTES must be positive. Falling back to 15.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_CLEANUP_INTERVAL_MINUTES", 15)

        if self.SUPPORT_MEDIA_PHOTO_MAX_MB <= 0:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_PHOTO_MAX_MB must be positive. Falling back to 2.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_PHOTO_MAX_MB", 2)

        if self.SUPPORT_MEDIA_AUDIO_MAX_MB <= 0:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_AUDIO_MAX_MB must be positive. Falling back to 20.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_AUDIO_MAX_MB", 20)

        if self.SUPPORT_MEDIA_VIDEO_MAX_MB <= 0:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_VIDEO_MAX_MB must be positive. Falling back to 50.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_VIDEO_MAX_MB", 50)

        if self.SUPPORT_MEDIA_VIDEO_MAX_MB < self.SUPPORT_MEDIA_PHOTO_MAX_MB:
            print(
                "[CONFIG WARNING] SUPPORT_MEDIA_VIDEO_MAX_MB should be >= SUPPORT_MEDIA_PHOTO_MAX_MB. "
                "Aligning video limit with photo limit.",
                file=sys.stderr,
            )
            object.__setattr__(self, "SUPPORT_MEDIA_VIDEO_MAX_MB", self.SUPPORT_MEDIA_PHOTO_MAX_MB)

        media_prefix = (self.SUPPORT_MEDIA_PUBLIC_PREFIX or "").strip() or "/support"
        if not media_prefix.startswith("/"):
            media_prefix = f"/{media_prefix}"
        object.__setattr__(self, "SUPPORT_MEDIA_PUBLIC_PREFIX", media_prefix)

        if self.DEVELOPER_OTP_LENGTH < 4 or self.DEVELOPER_OTP_LENGTH > 8:
            print(
                "[CONFIG WARNING] DEVELOPER_OTP_LENGTH must be between 4 and 8. Falling back to 6.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DEVELOPER_OTP_LENGTH", 6)

        if self.DEVELOPER_OTP_TTL_SECONDS <= 0:
            print(
                "[CONFIG WARNING] DEVELOPER_OTP_TTL_SECONDS must be positive. Falling back to 300.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DEVELOPER_OTP_TTL_SECONDS", 300)

        if self.DEVELOPER_OTP_MAX_VERIFY_ATTEMPTS <= 0:
            print(
                "[CONFIG WARNING] DEVELOPER_OTP_MAX_VERIFY_ATTEMPTS must be positive. Falling back to 5.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DEVELOPER_OTP_MAX_VERIFY_ATTEMPTS", 5)

        if self.DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS < 0:
            print(
                "[CONFIG WARNING] DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS cannot be negative. Falling back to 30.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DEVELOPER_OTP_RESEND_COOLDOWN_SECONDS", 30)

        if self.DEVELOPER_OTP_SESSION_TTL_SECONDS <= 0:
            print(
                "[CONFIG WARNING] DEVELOPER_OTP_SESSION_TTL_SECONDS must be positive. Falling back to 1800.",
                file=sys.stderr,
            )
            object.__setattr__(self, "DEVELOPER_OTP_SESSION_TTL_SECONDS", 1800)

        return self


settings = Settings()
