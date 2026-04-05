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

    # ── Telegram security alerts ─────────────────────────────────────────────
    SECURITY_ALERTS_ENABLED: bool = False
    SECURITY_ALERT_ON_SUCCESS_LOGIN: bool = True
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALERT_CHAT_ID: str = ""
    SECURITY_ALERT_TIMEOUT_SECONDS: float = 3.0
    ENABLE_IP_GEO_LOOKUP: bool = True
    IP_GEO_LOOKUP_TIMEOUT_SECONDS: float = 2.0
    ADMIN_BLOCK_LOGIN_ON_GEO_DENIED: bool = True

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

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of origins: "https://admin.GamerzAdda.com, http://localhost:3000"
    ALLOWED_ORIGINS: str = ""

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = ""

    # ── PayU ──────────────────────────────────────────────────────────────────
    PAYU_MERCHANT_KEY:  str = ""
    PAYU_MERCHANT_SALT: str = ""
    PAYU_BASE_URL:      str = ""
    PAYU_MERCHANT_VPA:  str = ""
    
    # ── Razorpay ──────────────────────────────────────────────────────────────
    RAZORPAY_KEY_ID:     str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_API_BASE_URL: str = ""

    # ── CCAvenue ──────────────────────────────────────────────────────────────
    CCAVENUE_MERCHANT_ID: str = ""
    CCAVENUE_ACCESS_CODE: str = ""
    CCAVENUE_WORKING_KEY: str = ""
    CCAVENUE_WEB_BASE_URL: str = ""
    CCAVENUE_API_BASE_URL: str = ""

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
        if not is_production:
            if not self.PAYU_BASE_URL:
                object.__setattr__(self, "PAYU_BASE_URL", "https://secure.payu.in")
            if not self.RAZORPAY_API_BASE_URL:
                object.__setattr__(self, "RAZORPAY_API_BASE_URL", "https://api.razorpay.com")
            if not self.CCAVENUE_WEB_BASE_URL:
                object.__setattr__(self, "CCAVENUE_WEB_BASE_URL", "https://secure.ccavenue.com")
            if not self.CCAVENUE_API_BASE_URL:
                object.__setattr__(self, "CCAVENUE_API_BASE_URL", "https://api.ccavenue.com")

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
                "PAYU_BASE_URL": self.PAYU_BASE_URL,
                "RAZORPAY_API_BASE_URL": self.RAZORPAY_API_BASE_URL,
                "CCAVENUE_WEB_BASE_URL": self.CCAVENUE_WEB_BASE_URL,
                "CCAVENUE_API_BASE_URL": self.CCAVENUE_API_BASE_URL,
            }
            missing = [key for key, value in required_fields.items() if not value]
            if missing:
                raise ValueError(
                    "Missing required production environment variables: " + ", ".join(missing)
                )

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
        
        # ── Razorpay Soft Warning ─────────────────────────────────────────────
        if not self.RAZORPAY_KEY_ID or not self.RAZORPAY_KEY_SECRET:
            print(
                "[CONFIG WARNING] Razorpay Key ID or Secret not set. "
                "Razorpay payments will fail.",
                file=sys.stderr
            )

        # ── CCAvenue Soft Warning ─────────────────────────────────────────────
        if not self.CCAVENUE_MERCHANT_ID or not self.CCAVENUE_ACCESS_CODE or not self.CCAVENUE_WORKING_KEY:
            print(
                "[CONFIG WARNING] CCAvenue Merchant ID, Access Code, or Working Key not set. "
                "CCAvenue payments will fail.",
                file=sys.stderr
            )

        if self.SECURITY_ALERTS_ENABLED and (not self.TELEGRAM_BOT_TOKEN or not self.TELEGRAM_ALERT_CHAT_ID):
            print(
                "[CONFIG WARNING] SECURITY_ALERTS_ENABLED is true but TELEGRAM_BOT_TOKEN/"
                "TELEGRAM_ALERT_CHAT_ID is missing. Disabling Telegram security alerts.",
                file=sys.stderr
            )
            object.__setattr__(self, "SECURITY_ALERTS_ENABLED", False)

        if self.IP_GEO_LOOKUP_TIMEOUT_SECONDS <= 0:
            print(
                "[CONFIG WARNING] IP_GEO_LOOKUP_TIMEOUT_SECONDS must be positive. Falling back to 2.0 seconds.",
                file=sys.stderr,
            )
            object.__setattr__(self, "IP_GEO_LOOKUP_TIMEOUT_SECONDS", 2.0)

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
