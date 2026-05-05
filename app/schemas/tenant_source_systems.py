"""
Pydantic schemas — tenant_source_systems endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import uuid

from pydantic import BaseModel, Field


# ── Existing schemas (unchanged) ──────────────────────────────────────────────

class TenantSourceSystemCheckRequest(BaseModel):
    tenant_id: uuid.UUID
    source_system_id: int


class TenantSourceSystemCheckResponse(BaseModel):
    exists: bool
    is_active: Optional[bool]
    message: str
    tenant_id: uuid.UUID
    source_system_id: int


class TenantActiveIntegrationsResponse(BaseModel):
    tenant_id: uuid.UUID
    active_source_system_ids: list[int]
    count: int


# ── New schema ────────────────────────────────────────────────────────────────

class TenantIntegrationDetailResponse(BaseModel):
    """
    Full integration detail returned by GET /tenant-source-systems/integration-detail.

    The `credentials` dict shape depends on auth_type:
      api_key / api_token / bearer_token → {"strategy": "api_token", "token": "..."}
      basic_auth                         → {"strategy": "basic", "username": "...", "password": "..."}
      oauth2                             → {"strategy": "oauth2", "access_token": "...", ...}

    `webhook_secrets` is the decrypted HMAC secret map (event → secret string),
    or null if the integration has no webhook secrets configured.
    """

    integration_id: str = Field(..., description="UUID of the crm_integrations row.")
    tenant_id: str       = Field(..., description="UUID of the tenant.")
    source_system_id: int

    crm_type: str        = Field(..., description="e.g. 'salesforce', 'hubspot'.")
    auth_type: str        = Field(..., description="e.g. 'api_key', 'oauth2'.")
    key_version: str     = Field(..., description="Infisical key version used (e.g. 'v1').")
    base_url: str

    webhook_uuid: Optional[str] = None
    is_active: bool

    credentials: Dict[str, Any] = Field(
        ...,
        description="Decrypted credentials dict — shape varies by auth_type.",
    )
    webhook_secrets: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Decrypted webhook HMAC secrets map, or null.",
    )

    token_expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None