# crm/credentials/models.py
"""
Credential Models
=================
Pydantic models that define the shape of credential payloads stored inside
Infisical secrets.

Design decisions
----------------
- Each auth strategy has its own model with required fields, so a mis-shaped
  credential dict is caught at read time, not mid-request when the HTTP call
  fails with a 401.
- `CrmCredentialEnvelope` is the top-level wrapper serialised to JSON and
  stored as the Infisical secret value.  It carries the adapter type so the
  factory can detect mismatches (e.g. someone stored Zammad creds for an
  EspoCRM integration).
- `base_url` lives in the envelope rather than in config YAML because it is
  tenant-specific — different customers may self-host at different domains.
- All models are exported through `crm/credentials/__init__.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Auth-strategy credential payloads
# ---------------------------------------------------------------------------

class ApiTokenCredentials(BaseModel):
    """Credentials for api_token authentication strategy."""
    strategy: Literal["api_token"] = "api_token"
    token: str = Field(..., min_length=1, description="The raw API token value.")


class BasicAuthCredentials(BaseModel):
    """Credentials for HTTP Basic authentication strategy."""
    strategy: Literal["basic"] = "basic"
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class OAuth2Credentials(BaseModel):
    """
    Credentials for OAuth2 authentication strategy.
    Refresh token is stored; the adapter exchanges it for an access token
    at runtime (access tokens are never persisted).
    """
    strategy: Literal["oauth2"] = "oauth2"
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    refresh_token: str = Field(..., min_length=1)
    token_url: str = Field(..., min_length=1, description="Full OAuth2 token endpoint URL.")


# Union type used by the envelope
AnyCredentials = Union[ApiTokenCredentials, BasicAuthCredentials, OAuth2Credentials]


# ---------------------------------------------------------------------------
# Top-level envelope — this is what gets JSON-serialised into Infisical
# ---------------------------------------------------------------------------

class CrmCredentialEnvelope(BaseModel):
    """
    The complete credential package stored as a single Infisical secret.

    Fields
    ------
    crm_type:
        Short lowercase CRM identifier matching crm_adapters.yaml
        (e.g. ``"zammad"``, ``"espocrm"``).
    base_url:
        Tenant-specific CRM instance URL (e.g. ``"https://support.acme.com"``).
    credentials:
        Auth-strategy-specific payload.  Serialised as a nested JSON object.
    metadata:
        Optional free-form dict for future use (e.g. rate-limit tier, region).
    """

    crm_type: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    credentials: Dict[str, Any] = Field(
        ...,
        description=(
            "Flat dict representation of one of ApiTokenCredentials, "
            "BasicAuthCredentials, or OAuth2Credentials."
        ),
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def base_url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"base_url must start with http:// or https://, got: '{v}'"
            )
        return v.rstrip("/")

    @field_validator("crm_type")
    @classmethod
    def crm_type_lowercase(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def credentials_have_strategy(self) -> "CrmCredentialEnvelope":
        if "strategy" not in self.credentials:
            raise ValueError(
                "credentials dict must contain a 'strategy' key "
                "(one of: api_token, basic, oauth2)."
            )
        allowed = {"api_token", "basic", "oauth2"}
        strat = self.credentials["strategy"]
        if strat not in allowed:
            raise ValueError(
                f"credentials.strategy must be one of {allowed}, got '{strat}'."
            )
        return self

    def to_credential_dict(self) -> Dict[str, Any]:
        """
        Return the raw credentials dict — passed directly into BaseCrmClient.
        Strips the 'strategy' key since the client reads strategy from config.
        """
        return {k: v for k, v in self.credentials.items() if k != "strategy"}


# ---------------------------------------------------------------------------
# Settings model — read from environment variables at startup
# ---------------------------------------------------------------------------

class InfisicalSettings(BaseModel):
    """
    Configuration for the InfisicalCredentialManager, populated from env vars.

    Required environment variables
    --------------------------------
    INFISICAL_CLIENT_ID        Machine Identity client ID
    INFISICAL_CLIENT_SECRET    Machine Identity client secret
    INFISICAL_PROJECT_ID       Infisical project (workspace) ID
    INFISICAL_ENVIRONMENT      Secret environment slug, e.g. ``"prod"``

    Optional environment variables
    --------------------------------
    INFISICAL_HOST             Default: ``"https://app.infisical.com"``
    INFISICAL_SECRET_PATH      Default path for secrets, e.g. ``"/crm"``
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
        Construct from environment variables.
        Raises ``InfisicalConfigError`` (not ValidationError) on missing vars
        so callers get a meaningful startup message.
        """
        import os
        from crm.credentials.exceptions import InfisicalConfigError

        required = {
            "client_id": "INFISICAL_CLIENT_ID",
            "client_secret": "INFISICAL_CLIENT_SECRET",
            "project_id": "INFISICAL_PROJECT_ID",
        }
        missing = [env for field, env in required.items() if not os.getenv(env)]
        if missing:
            raise InfisicalConfigError(
                f"Missing required environment variables for Infisical: {missing}. "
                "Set them before starting the application."
            )

        return cls(
            client_id=os.environ["INFISICAL_CLIENT_ID"],
            client_secret=os.environ["INFISICAL_CLIENT_SECRET"],
            project_id=os.environ["INFISICAL_PROJECT_ID"],
            environment=os.getenv("INFISICAL_ENVIRONMENT", "prod"),
            host=os.getenv("INFISICAL_HOST", "https://app.infisical.com"),
            secret_path=os.getenv("INFISICAL_SECRET_PATH", "/crm"),
        )