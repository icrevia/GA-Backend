from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "ZexPlay"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "ZexPlay_Super_Secure_JWT_Key_2026_@"  # In prod, this should be in .env
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    APP_URL: str = "https://web-production-051ba.up.railway.app"
    
    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/zexplay_db"
    
    # PayU Configuration
    PAYU_MERCHANT_KEY: str = "TEST_MERCHANT_KEY"
    PAYU_MERCHANT_SALT: str = "TEST_MERCHANT_SALT"
    PAYU_BASE_URL: str = "https://test.payu.in" # Use test environment by default

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
