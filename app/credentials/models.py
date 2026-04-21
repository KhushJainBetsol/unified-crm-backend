# app/credentials/models.py
"""
Credential models for the credential management layer.

CrmCredentialEnvelope  — full envelope stored in Infisical (new adapter pattern)
InfisicalSettings      — SDK configuration, populated from env vars
"""

from __future__ import annotations

import os
from typing import Any, Dict
from pydantic import BaseModel, Field, field_validator, model_validator

from app.credentials.exceptions import InfisicalConfigError


# ---------------------------------------------------------------------------
# CrmCredentialEnvelope
# ---------------------------------------------------------------------------

class CrmCredentialEnvelope(BaseModel):
    """
    The complete credential package serialised as a single Infisical secret.

    Fields
    ------
    crm_type   : matches a key in crm_adapters.yaml  (e.g. "zammad", "espocrm")
    base_url   : tenant-specific CRM instance URL  (NOT stored in the DB)
    credentials: auth-strategy dict — must contain a 'strategy' key
    metadata   : optional free-form dict (rate-limit tier, region, etc.)

    Secret name in Infisical: CREDS_<integration_id>
    Database stores         : integration_id only
    """

    crm_type: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    credentials: Dict[str, Any] = Field(
        ...,
        description=(
            "Auth credentials dict. Must include 'strategy' key. "
            "Strategies: api_token | basic | oauth2"
        ),
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("crm_type")
    @classmethod
    def crm_type_lowercase(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("base_url")
    @classmethod
    def base_url_must_have_scheme(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"base_url must start with http:// or https://, got: '{v}'"
            )
        return v.rstrip("/")

    @model_validator(mode="after")
    def credentials_must_have_strategy(self) -> "CrmCredentialEnvelope":
        if "strategy" not in self.credentials:
            raise ValueError(
                "credentials dict must contain a 'strategy' key. "
                "Valid values: api_token, basic, oauth2"
            )
        allowed = {"api_token", "basic", "oauth2"}
        strat = self.credentials["strategy"]
        if strat not in allowed:
            raise ValueError(
                f"credentials.strategy must be one of {allowed}, got '{strat}'"
            )
        return self

    # ------------------------------------------------------------------
    # Helper used by CrmAdapterFactory
    # ------------------------------------------------------------------

    def to_credential_dict(self) -> Dict[str, Any]:
        """
        Return a clean credential dict for injection into BaseCrmClient.
        Strips 'strategy' — the client reads strategy from AdapterConfig, not here.
        """
        return {k: v for k, v in self.credentials.items() if k != "strategy"}


# ---------------------------------------------------------------------------
# InfisicalSettings
# ---------------------------------------------------------------------------

class InfisicalSettings(BaseModel):
    """
    SDK configuration for InfisicalCredentialManager.

    Required env vars
    -----------------
    INFISICAL_CLIENT_ID
    INFISICAL_CLIENT_SECRET
    INFISICAL_PROJECT_ID

    Optional env vars (defaults shown)
    -----------------------------------
    INFISICAL_ENVIRONMENT   prod
    INFISICAL_HOST          https://app.infisical.com
    INFISICAL_SITE_URL      (alias for INFISICAL_HOST, takes priority)
    INFISICAL_SECRET_PATH   /crm
    """

    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    environment: str = Field(default="prod")
    host: str = Field(default="https://app.infisical.com")
    secret_path: str = Field(default="/crm")

    @classmethod
    def from_env(cls) -> "InfisicalSettings":
        """
        Build from environment variables.
        Strips whitespace to handle shell export formatting quirks.
        Raises InfisicalConfigError (not ValidationError) on missing vars.
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

        # INFISICAL_SITE_URL takes priority over INFISICAL_HOST
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