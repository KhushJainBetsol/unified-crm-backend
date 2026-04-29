"""
Schemas for tenant_source_system check endpoint.
"""

from __future__ import annotations

import uuid
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Request                                                                       #
# --------------------------------------------------------------------------- #

class TenantSourceSystemCheckRequest(BaseModel):
    tenant_id: uuid.UUID = Field(
        ...,
        description="UUID of the tenant to look up.",
        example="3fa85f64-5717-4562-b3fc-2c963f66afa6",
    )
    source_system_id: int = Field(
        ...,
        description="Integer ID of the source system to look up.",
        example=1,
    )


# --------------------------------------------------------------------------- #
# Response — single check                                                       #
# --------------------------------------------------------------------------- #

class TenantSourceSystemCheckResponse(BaseModel):
    exists: bool = Field(
        ...,
        description="True if the (tenant_id, source_system_id) pair exists in the table.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Active flag of the record. None when the record does not exist.",
    )
    message: str = Field(
        ...,
        description="Human-readable result message.",
    )
    tenant_id: uuid.UUID = Field(..., description="Echo of the requested tenant_id.")
    source_system_id: int = Field(..., description="Echo of the requested source_system_id.")


# --------------------------------------------------------------------------- #
# Response — active integrations list (new)                                     #
# --------------------------------------------------------------------------- #

class ActiveIntegrationItem(BaseModel):
    """A single active source-system integration belonging to the tenant."""
    source_system_id: int = Field(
        ...,
        description="Integer ID of the source system.",
    )

class TenantActiveIntegrationsResponse(BaseModel):
    tenant_id: uuid.UUID = Field(
        ...,
        description="The tenant whose integrations are listed.",
    )
    active_source_system_ids: list[int] = Field(
        default_factory=list,
        description=(
            "List of source_system_id values where the tenant has an "
            "active (is_active=True) mapping."
        ),
    )
    count: int = Field(
        ...,
        description="Number of active integrations found.",
    )