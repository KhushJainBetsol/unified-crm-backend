"""
Router — tenant_source_systems check.

Exposes two routes for flexibility:
  POST /tenant-source-systems/check   — body-based (preferred for programmatic use)
  GET  /tenant-source-systems/check   — query-param-based (handy for quick browser/curl checks)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db  # your existing async session dependency
from app.schemas.tenant_source_systems import (
    TenantSourceSystemCheckRequest,
    TenantSourceSystemCheckResponse,
    TenantActiveIntegrationsResponse
)
from app.services.tenant_source_systems import check_tenant_source_system,get_active_integrations

router = APIRouter(
    prefix="/tenant-source-systems",
    tags=["Tenant Source Systems"],
)

# --------------------------------------------------------------------------- #
# GET /tenant-source-systems/check                                              #
# --------------------------------------------------------------------------- #

@router.get(
    "/check",
    response_model=TenantSourceSystemCheckResponse,
    summary="Check if a tenant↔source-system mapping exists (query params)",
    response_description="exists=True/False with a descriptive message.",
)
async def check_mapping_get(
    tenant_id: uuid.UUID = Query(..., description="UUID of the tenant."),
    source_system_id: int = Query(..., description="Integer ID of the source system."),
    db: AsyncSession = Depends(get_db),
) -> TenantSourceSystemCheckResponse:
    """
    Same logic as the POST variant but accepts inputs as **query parameters**,
    making it easy to test directly from a browser or curl:

    ```
    GET /tenant-source-systems/check?tenant_id=<uuid>&source_system_id=1
    ```
    """
    request = TenantSourceSystemCheckRequest(
        tenant_id=tenant_id,
        source_system_id=source_system_id,
    )
    return await check_tenant_source_system(request, db)

# --------------------------------------------------------------------------- #
# GET /tenant-source-systems/active  (NEW)                                      #
# --------------------------------------------------------------------------- #

@router.get(
    "/active",
    response_model=TenantActiveIntegrationsResponse,
    summary="List all active source-system integrations for a tenant",
    response_description=(
        "Returns the tenant_id plus a list of source_system_ids "
        "that are currently active for that tenant."
    ),
)
async def get_active_integrations_for_tenant(
    tenant_id: uuid.UUID = Query(..., description="UUID of the tenant."),
    db: AsyncSession = Depends(get_db),
) -> TenantActiveIntegrationsResponse:
    # request = TenantActiveIntegrationsResponse(
    #     tenant_id=tenant_id,
    # )
    return await get_active_integrations(tenant_id, db)
    