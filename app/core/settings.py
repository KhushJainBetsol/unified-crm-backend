"""
Centralised environment configuration using pydantic-settings.
All values are loaded from environment variables or the .env file.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # silently ignore unknown env vars
    )

    # ----------------------------------------------------------------
    # App
    # ----------------------------------------------------------------
    APP_NAME: str = "UnifiedCRM"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # ----------------------------------------------------------------
    # Database
    # ----------------------------------------------------------------
    DATABASE_URL: str


    # ----------------------------------------------------------------
    # JWT / Auth
    # ----------------------------------------------------------------
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ----------------------------------------------------------------
    # EspoCRM
    # ----------------------------------------------------------------
    ESPO_BASE_URL: str = ""
    ESPO_API_KEY: str = ""

    # ----------------------------------------------------------------
    # Zammad
    # ----------------------------------------------------------------
    ZAMMAD_BASE_URL: str = ""
    ZAMMAD_API_TOKEN: str = ""


@lru_cache
def get_settings() -> Settings:
    """
    Return a cached Settings instance.
    Instantiated once per process — safe to call anywhere without overhead.
    """
    return Settings()  # type: ignore[call-arg]