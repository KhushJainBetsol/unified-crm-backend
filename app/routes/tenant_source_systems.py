"""
Router — tenant_source_systems.

Routes
------
  GET /tenant-source-systems/check
      Check if a (tenant_id, source_system_id) mapping exists.

  GET /tenant-source-systems/active
      List all active source-system IDs for a tenant.

  GET /tenant-source-systems/integration-detail          ← NEW
      Resolve the integration from (tenant_id, source_system_id),
      decrypt its credentials, and return the full detail JSON.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_key_manager          # add get_key_manager here
from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.schemas.tenant_source_systems import (
    TenantSourceSystemCheckRequest,
    TenantSourceSystemCheckResponse,
    TenantActiveIntegrationsResponse,
    TenantIntegrationDetailResponse,
)
from app.services.tenant_source_systems import (
    check_tenant_source_system,
    get_active_integrations,
    get_integration_detail,
)

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
    request = TenantSourceSystemCheckRequest(
        tenant_id=tenant_id,
        source_system_id=source_system_id,
    )
    return await check_tenant_source_system(request, db)


# --------------------------------------------------------------------------- #
# GET /tenant-source-systems/active                                             #
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
    return await get_active_integrations(tenant_id, db)


# --------------------------------------------------------------------------- #
# GET /tenant-source-systems/integration-detail       ← NEW                    #
# --------------------------------------------------------------------------- #

@router.get(
    "/integration-detail",
    response_model=TenantIntegrationDetailResponse,
    summary="Get full integration detail with decrypted credentials",
    response_description=(
        "Resolves the crm_integration for (tenant_id, source_system_id), "
        "decrypts credentials and webhook secrets, and returns the full JSON."
    ),
)
async def get_integration_detail_endpoint(
    tenant_id: uuid.UUID = Query(..., description="UUID of the tenant."),
    source_system_id: int = Query(..., description="Integer ID of the source system."),
    db: AsyncSession = Depends(get_db),
    key_manager: AsyncInfisicalCredentialManager = Depends(get_key_manager),
) -> TenantIntegrationDetailResponse:
    """
    Full resolution flow:

    1. `tenant_source_systems` → `integration_id`
    2. `crm_integrations` row  → encrypted blobs + metadata
    3. Infisical               → AES key (tenant-versioned or global fallback)
    4. AES-256-CBC decrypt     → plaintext credentials + webhook secrets
    5. Return structured JSON

    Example response:
    ```json
    {
      "integration_id": "3fc6c469-...",
      "tenant_id": "e921a37f-...",
      "source_system_id": 2,
      "crm_type": "freshdesk",
      "auth_type": "api_key",
      "key_version": "v1",
      "base_url": "http://192.168.80.229:9091",
      "webhook_uuid": "5b57a3cd-...",
      "is_active": true,
      "credentials": {
        "strategy": "api_token",
        "token": "your-decrypted-api-key"
      },
      "webhook_secrets": {
        "Case.create": "secret1",
        "Case.update": "secret2"
      },
      "token_expires_at": null,
      "created_at": "2026-05-04T06:06:05.135924-04:00",
      "updated_at": "2026-05-04T06:06:05.135924-04:00"
    }
    ```
    """
    return await get_integration_detail(
        tenant_id=tenant_id,
        source_system_id=source_system_id,
        db=db,
        key_manager=key_manager,
    )
    