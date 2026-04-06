"""
app/core/settings.py

Centralised environment configuration using pydantic-settings.

New vars added for Keycloak multitenancy (marked with # KEYCLOAK NEW).
All existing vars are unchanged.
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    APP_NAME: str = "UnifiedCRM"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str

    # JWT (legacy — keep for any existing helpers, not used for auth anymore)
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # EspoCRM
    ESPO_BASE_URL: str = ""
    ESPO_API_KEY: str = ""

    # Zammad
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

    # ── KEYCLOAK NEW ──────────────────────────────────────────────────────────
    KEYCLOAK_URL: str = "http://localhost:8080"
    KEYCLOAK_REALM: str = "unified-crm"
    # Client used by the frontend (public PKCE client)
    KEYCLOAK_CLIENT_ID: str = "crm-frontend"
    # Service-account client used by backend to call Keycloak Admin API
    KEYCLOAK_ADMIN_CLIENT_ID: str = "crm-admin-api"
    KEYCLOAK_ADMIN_CLIENT_SECRET: str = ""
    # Frontend URL — used when generating invite links
    FRONTEND_URL: str = "http://localhost:5173"
    # Super admin email — identified by this address until superadmin role is ready
    SUPER_ADMIN_EMAIL: str = ""
    # ── END KEYCLOAK NEW ──────────────────────────────────────────────────────

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]