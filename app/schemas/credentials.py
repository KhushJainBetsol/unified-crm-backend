"""
app/schemas/credentials.py

Request / response schemas for the credential provisioning API.

Each CRM auth_type has its own strongly-typed request body so the
API surface is self-documenting and validated before any crypto runs.

Hierarchy
---------
CredentialRequest           ← discriminated union (the actual POST body)
  ├── ApiTokenCredentials   ← auth_type = api_token | bearer_token | access_token | api_key
  ├── BasicAuthCredentials  ← auth_type = basic_auth
  ├── OAuth2Credentials     ← auth_type = oauth2
  └── HmacCredentials       ← auth_type = hmac (outbound api_token only)

ProvisionCredentialsRequest ← full request body wrapping a CredentialRequest.
                              Carries optional top-level webhook_secret /
                              per_event_secrets for ALL auth types — these are
                              stored encrypted in webhook_secrets_enc (separate
                              column from credential_enc).

CredentialStatusResponse    ← lightweight response (never echoes secrets back)

Two-column secret model
-----------------------
credential_enc      → outbound auth secrets  (api token, password, OAuth tokens …)
webhook_secrets_enc → inbound webhook secrets (HMAC shared secret, per-event secrets)

For hmac auth_type the outbound api_token goes into credential_enc while
webhook_secret / per_event_secrets go into webhook_secrets_enc.
For every other auth_type the credentials go into credential_enc and the
top-level webhook_secret / per_event_secrets (if supplied) go into
webhook_secrets_enc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, Literal, Optional, Union
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Per-auth-type credential payloads
# ---------------------------------------------------------------------------

class ApiTokenCredentials(BaseModel):
    """
    Works for: api_token, bearer_token, access_token, api_key.
    Examples: Zammad API token, EspoCRM API key, Stripe secret key.
    """
    auth_type: Literal["api_token", "bearer_token", "access_token", "api_key"] = "api_token"
    token: str = Field(..., min_length=1, description="The raw API token / key string.")

    def to_secret_dict(self) -> dict:
        """Outbound secrets stored in credential_enc."""
        return {"token": self.token}

    def to_webhook_secret_dict(self) -> dict | None:
        """No webhook secrets embedded in this payload — always None."""
        return None


class BasicAuthCredentials(BaseModel):
    """HTTP Basic auth — username + password."""
    auth_type: Literal["basic_auth"] = "basic_auth"
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)

    def to_secret_dict(self) -> dict:
        return {"username": self.username, "password": self.password}

    def to_webhook_secret_dict(self) -> dict | None:
        return None


class OAuth2Credentials(BaseModel):
    """
    OAuth 2.0 tokens.
    access_token is required; refresh_token and expiry are optional
    (some CRMs issue non-expiring tokens).
    """
    auth_type: Literal["oauth2"] = "oauth2"
    access_token: str = Field(..., min_length=1)
    refresh_token: Optional[str] = None
    token_type: str = Field(default="Bearer")
    expires_at: Optional[int] = Field(
        default=None,
        description="Unix timestamp of access_token expiry. NULL = non-expiring.",
    )
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def to_secret_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

    def to_webhook_secret_dict(self) -> dict | None:
        return None


class HmacCredentials(BaseModel):
    """
    HMAC credentials.

    Outbound (→ credential_enc):
        api_token — the API token used to authenticate outbound calls to the CRM.

    Inbound (→ webhook_secrets_enc):
        webhook_secret    — Zammad-style single shared secret.
        per_event_secrets — EspoCRM-style per-event secrets dict.

    At least one of the three fields must be supplied.
    """
    auth_type: Literal["hmac"] = "hmac"

    # Outbound auth
    api_token: Optional[str] = Field(
        default=None,
        description="Outbound API token for CRM API calls.",
    )
    # Inbound webhook verification
    webhook_secret: Optional[str] = Field(
        default=None,
        description="Shared HMAC secret for inbound webhook verification (Zammad-style).",
    )
    per_event_secrets: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Per-event HMAC secrets (EspoCRM-style). "
            'e.g. {"Case.create": "s1", "Case.update": "s2"}'
        ),
    )

    @model_validator(mode="after")
    def at_least_one_secret(self) -> "HmacCredentials":
        if not any([self.api_token, self.webhook_secret, self.per_event_secrets]):
            raise ValueError(
                "HmacCredentials requires at least one of: "
                "api_token, webhook_secret, per_event_secrets"
            )
        return self

    def to_secret_dict(self) -> dict:
        """
        Outbound secrets only → stored in credential_enc.
        Webhook secrets are split off into to_webhook_secret_dict().
        """
        return {"api_token": self.api_token}

    def to_webhook_secret_dict(self) -> dict | None:
        """
        Inbound webhook secrets → stored in webhook_secrets_enc.
        Returns None if neither field was supplied.
        """
        if not self.webhook_secret and not self.per_event_secrets:
            return None
        return {
            "webhook_secret": self.webhook_secret,
            "per_event_secrets": self.per_event_secrets or {},
        }


# Discriminated union — FastAPI resolves the correct model from auth_type
CredentialPayload = Annotated[
    Union[
        ApiTokenCredentials,
        BasicAuthCredentials,
        OAuth2Credentials,
        HmacCredentials,
    ],
    Field(discriminator="auth_type"),
]


# ---------------------------------------------------------------------------
# Main request bodies
# ---------------------------------------------------------------------------

class ProvisionCredentialsRequest(BaseModel):
    """
    POST /api/v1/integrations/

    The `credentials` field is a discriminated union — FastAPI/Pydantic selects
    the correct model based on `credentials.auth_type`.

    Webhook secrets can be supplied in two ways:
      - For hmac auth_type: embed them inside the `credentials` payload.
      - For any auth_type: supply top-level `webhook_secret` / `per_event_secrets`.
        These are always stored encrypted in the separate `webhook_secrets_enc` column,
        completely independent of the outbound `credential_enc`.

    Example — Zammad API token with a webhook secret:
        {
            "base_url": "https://crm.example.com",
            "crm_type": "zammad",
            "credentials": {
                "auth_type": "api_token",
                "token": "my-real-token"
            },
            "webhook_secret": "my-hmac-secret"
        }

    Example — EspoCRM HMAC (outbound token + per-event inbound secrets):
        {
            "base_url": "https://espo.example.com",
            "crm_type": "espocrm",
            "credentials": {
                "auth_type": "hmac",
                "api_token": "f177888efa9b...",
                "per_event_secrets": {
                    "Case.create": "secret1",
                    "Case.update": "secret2"
                }
            }
        }
    """
    crm_type: str = Field(..., min_length=1, description="CRM type key (e.g. 'zammad', 'espocrm').")
    base_url: AnyHttpUrl = Field(..., description="Tenant-specific CRM instance URL.")
    credentials: CredentialPayload

    # ── Top-level webhook secrets (applicable to any auth_type) ───────────
    # For hmac auth_type these are also accepted inside `credentials`;
    # the service merges them, preferring `credentials` values when both present.
    webhook_secret: Optional[str] = Field(
        default=None,
        description=(
            "Inbound webhook HMAC secret. Stored encrypted in webhook_secrets_enc. "
            "Can be null — the column is nullable."
        ),
    )
    per_event_secrets: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Per-event inbound webhook secrets. Stored encrypted in webhook_secrets_enc. "
            'e.g. {"Case.create": "s1", "Case.update": "s2"}'
        ),
    )

    # Optional non-sensitive metadata (region, scopes, etc.)
    extra_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional extra non-sensitive config stored alongside the integration.",
    )

    def build_webhook_secret_dict(self) -> dict | None:
        """
        Merge webhook secrets from the credential payload (hmac) and top-level fields.
        Returns None when no webhook secrets were provided at all.
        """
        # Start from the credential payload (only HmacCredentials populates this)
        base: dict = {}
        if hasattr(self.credentials, "to_webhook_secret_dict"):
            cred_ws = self.credentials.to_webhook_secret_dict()
            if cred_ws:
                base.update(cred_ws)

        # Top-level fields override / supplement
        if self.webhook_secret is not None:
            base["webhook_secret"] = self.webhook_secret
        if self.per_event_secrets is not None:
            base["per_event_secrets"] = self.per_event_secrets

        return base if base else None


class UpdateCredentialsRequest(BaseModel):
    """
    PATCH /api/v1/integrations/{integration_id}/credentials
    Partial update — only fields present in the request body are changed.
    """
    base_url: Optional[AnyHttpUrl] = None
    credentials: Optional[CredentialPayload] = None

    # Webhook secrets can be updated independently of outbound credentials
    webhook_secret: Optional[str] = Field(
        default=None,
        description="Set or replace the inbound webhook HMAC secret.",
    )
    per_event_secrets: Optional[Dict[str, str]] = Field(
        default=None,
        description="Set or replace per-event inbound webhook secrets.",
    )

    extra_metadata: Optional[Dict[str, Any]] = None

    def has_webhook_updates(self) -> bool:
        """True when any webhook secret field was explicitly supplied."""
        return self.webhook_secret is not None or self.per_event_secrets is not None

    def build_webhook_secret_dict(self) -> dict | None:
        """
        Build the webhook secrets dict from this update request.
        Only call after has_webhook_updates() returns True.
        """
        base: dict = {}
        if hasattr(self.credentials, "to_webhook_secret_dict") and self.credentials:
            cred_ws = self.credentials.to_webhook_secret_dict()
            if cred_ws:
                base.update(cred_ws)
        if self.webhook_secret is not None:
            base["webhook_secret"] = self.webhook_secret
        if self.per_event_secrets is not None:
            base["per_event_secrets"] = self.per_event_secrets
        return base if base else None


# ---------------------------------------------------------------------------
# Response schemas — NEVER echo secrets back
# ---------------------------------------------------------------------------

class CredentialStatusResponse(BaseModel):
    """
    Returned after provisioning or on GET /status.
    Contains zero secret material — only audit/status info.
    """
    integration_id: UUID
    crm_type: str
    auth_type: str
    base_url: str
    key_version: str
    is_active: bool
    has_credentials: bool
    has_webhook_secrets: bool          # True when webhook_secrets_enc is populated
    token_expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CredentialRotationResponse(BaseModel):
    """Returned after a credential rotation."""
    integration_id: UUID
    old_key_version: str
    new_key_version: str
    rotated_at: datetime
    message: str = "Credentials rotated successfully."