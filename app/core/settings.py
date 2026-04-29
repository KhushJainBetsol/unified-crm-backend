"""
app/core/settings.py

Centralised environment configuration using pydantic-settings.
Includes a pre-validator to strip hidden carriage returns (\r) common in RHEL/Windows transfers.
"""

from functools import lru_cache
from typing import Literal, Any, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,   # ← FIXED: False caused RHEL env var matching issues
        extra="ignore",
    )

    # ── CLEANING DATA ────────────────────────────────────────────────────────
    @field_validator("*", mode="before")
    @classmethod
    def strip_whitespace(cls, v: Any) -> Any:
        """Strip hidden characters like \r and trailing spaces from all input values."""
        if isinstance(v, str):
            return v.strip()
        return v

    # ── App ──────────────────────────────────────────────────────────────────
    APP_NAME: str = "UnifiedCRM"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str

    # JWT (legacy)
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # EspoCRM
    # NOTE: Legacy env vars, kept for backwards compatibility only.
    # All new code uses the adapter pattern with credentials from CRM integrations table.
    ESPO_BASE_URL: str = ""
    ESPO_API_KEY: str = ""

    # Zammad
    # NOTE: Legacy env vars, kept for backwards compatibility only.
    # All new code uses the adapter pattern with credentials from CRM integrations table.
    ZAMMAD_BASE_URL: str = ""
    ZAMMAD_API_TOKEN: str = ""

    WEBHOOK_TENANT_ID: str = ""
    ESPO_WEBHOOK_UUID: str = ""
    ESPO_SECRET_CASE_CREATE: str = ""
    ESPO_SECRET_CASE_UPDATE: str = ""
    ESPO_SECRET_CASE_DELETE: str = ""
    ZAMMAD_WEBHOOK_UUID: str = ""
    ZAMMAD_WEBHOOK_SECRET: str = ""

    SYNC_INTERVAL_MINUTES: int = 15

    # ── KEYCLOAK ──────────────────────────────────────────────────────────────
    KEYCLOAK_URL: str = "http://localhost:8080"
    KEYCLOAK_REALM: str = "unified-crm"
    KEYCLOAK_CLIENT_ID: str = "crm-frontend"
    KEYCLOAK_ADMIN_CLIENT_ID: str = "crm-admin-api"
    KEYCLOAK_ADMIN_CLIENT_SECRET: str = ""
    FRONTEND_URL: str = "http://localhost:5173"
    SUPER_ADMIN_EMAIL: str = ""

    # Adapter pattern (using adapter engine for all new operations)
    CRM_CONFIG_DIR: str = "app/config"
    CRM_ADAPTER_ENGINE: str = "new"  # Default to new adapter pattern

    # ── Infisical ─────────────────────────────────────────────────────────────
    # No empty-string defaults — if these are missing pydantic raises a clear
    # ValidationError at startup rather than silently passing "" to the SDK.
    INFISICAL_CLIENT_ID: str
    INFISICAL_CLIENT_SECRET: str
    INFISICAL_PROJECT_ID: str
    INFISICAL_ENVIRONMENT: str = "dev"
    INFISICAL_HOST: str = "https://app.infisical.com"
    INFISICAL_SECRET_PATH: str = "/"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]