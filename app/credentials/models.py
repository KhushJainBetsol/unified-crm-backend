# app/credentials/models.py
"""
Credential models for the credential management layer.

CrmCredentialEnvelope  — full envelope built in-memory at request time
InfisicalSettings      — SDK configuration

CONSTRUCTION RULES
------------------
Inside FastAPI:      InfisicalSettings.from_app_settings(get_settings())
Standalone scripts:  InfisicalSettings.from_env()
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from pydantic import BaseModel, Field, field_validator, model_validator

from app.credentials.exceptions import InfisicalConfigError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CrmCredentialEnvelope
# ---------------------------------------------------------------------------

class CrmCredentialEnvelope(BaseModel):
    """
    The complete credential package built in-memory at request time.
    Never persisted — lives only for the duration of one request/operation.
    """

    crm_type: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    credentials: Dict[str, Any] = Field(
        ...,
        description="Auth credentials dict. Must include 'strategy' key.",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("crm_type")
    @classmethod
    def crm_type_lowercase(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("base_url")
    @classmethod
    def base_url_must_have_scheme(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url must start with http:// or https://, got: '{v}'")
        return v.rstrip("/")

    @model_validator(mode="after")
    def credentials_must_have_strategy(self) -> "CrmCredentialEnvelope":
        allowed = {"api_token", "basic", "oauth2"}
        if "strategy" not in self.credentials:
            raise ValueError(f"credentials dict must contain a 'strategy' key. Valid: {allowed}")
        if self.credentials["strategy"] not in allowed:
            raise ValueError(f"credentials.strategy must be one of {allowed}")
        return self

    def to_credential_dict(self) -> Dict[str, Any]:
        """Strip 'strategy' — safe for injection into BaseCrmClient."""
        return {k: v for k, v in self.credentials.items() if k != "strategy"}


# ---------------------------------------------------------------------------
# InfisicalSettings
# ---------------------------------------------------------------------------

class InfisicalSettings(BaseModel):
    """
    SDK configuration for InfisicalCredentialManager.

    Always prefer from_app_settings() inside FastAPI.
    Use from_env() only in standalone scripts.
    """

    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    environment: str = Field(default="dev")
    host: str = Field(default="https://app.infisical.com")
    secret_path: str = Field(default="/")

    # ------------------------------------------------------------------
    # PREFERRED — use inside FastAPI
    # ------------------------------------------------------------------

    @classmethod
    def from_app_settings(cls, app_settings: Any) -> "InfisicalSettings":
        """
        Build from the app's already-loaded pydantic-settings Settings object.

        pydantic-settings reads and validates .env at startup. This method
        maps the already-resolved values across — no os.getenv() calls.

        Logs the resolved values (secrets masked) so misconfiguration is
        immediately visible in startup logs.
        """
        # Read directly from the Settings attributes — these are already
        # validated and stripped by the Settings field_validator.
        client_id     = str(app_settings.INFISICAL_CLIENT_ID).strip()
        client_secret = str(app_settings.INFISICAL_CLIENT_SECRET).strip()
        project_id    = str(app_settings.INFISICAL_PROJECT_ID).strip()
        environment   = str(app_settings.INFISICAL_ENVIRONMENT).strip()
        host          = str(app_settings.INFISICAL_HOST).strip()
        secret_path   = str(getattr(app_settings, "INFISICAL_SECRET_PATH", "/")).strip()

        # Log resolved values so startup issues are immediately visible.
        # Secrets are masked — only first 6 chars shown.
        logger.info(
            "InfisicalSettings resolved from app_settings: "
            "host=%s project_id=%s environment=%s secret_path=%s "
            "client_id=%s... client_secret=%s...",
            host, project_id, environment, secret_path,
            client_id[:6] if client_id else "EMPTY",
            client_secret[:6] if client_secret else "EMPTY",
        )

        # Fail with a clear message if any required field is empty.
        # With required fields (no default="") in Settings, pydantic would
        # have already raised at startup — this is a last-resort safety net.
        missing = [
            name for name, val in [
                ("INFISICAL_CLIENT_ID",     client_id),
                ("INFISICAL_CLIENT_SECRET", client_secret),
                ("INFISICAL_PROJECT_ID",    project_id),
            ]
            if not val
        ]
        if missing:
            raise InfisicalConfigError(
                f"Missing or empty Infisical settings after resolution: {missing}. "
                "Check your .env file and ensure pydantic-settings is reading it correctly."
            )

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            project_id=project_id,
            environment=environment,
            host=host,
            secret_path=secret_path,
        )

    # ------------------------------------------------------------------
    # FALLBACK — use only in standalone scripts / CLI tools
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "InfisicalSettings":
        """
        Build from os.getenv().

        Use ONLY in standalone scripts (migrate_credential_columns.py,
        demo_credential_flow.py) that run outside FastAPI and load dotenv
        themselves. Inside FastAPI, use from_app_settings() instead.
        """
        required = {
            "client_id":     "INFISICAL_CLIENT_ID",
            "client_secret": "INFISICAL_CLIENT_SECRET",
            "project_id":    "INFISICAL_PROJECT_ID",
        }
        values = {k: os.getenv(env, "").strip() for k, env in required.items()}
        missing = [env for k, env in required.items() if not values[k]]

        if missing:
            raise InfisicalConfigError(
                f"Missing or empty environment variables: {missing}. "
                "Set them in your .env file or export them in your shell."
            )

        host = (
            os.getenv("INFISICAL_SITE_URL", "").strip()
            or os.getenv("INFISICAL_HOST", "https://app.infisical.com").strip()
        )

        return cls(
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            project_id=values["project_id"],
            environment=os.getenv("INFISICAL_ENVIRONMENT", "dev").strip(),
            host=host,
            secret_path=os.getenv("INFISICAL_SECRET_PATH", "/").strip(),
        )