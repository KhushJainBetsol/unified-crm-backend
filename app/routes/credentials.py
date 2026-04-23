# app/routes/credentials.py
"""
REST API for CRM credential lifecycle.

Endpoints
---------
POST   /api/v1/integrations/                               → provision
PATCH  /api/v1/integrations/{integration_id}/credentials   → partial update
GET    /api/v1/integrations/{integration_id}/credentials/status
POST   /api/v1/integrations/{integration_id}/credentials/rotate
DELETE /api/v1/integrations/{integration_id}/credentials   → revoke

Auth
----
All write endpoints require a valid Keycloak JWT.
tenant_id is extracted from the JWT claims — never from the request body.
Secrets are NEVER returned in any response.

Verification gate (provision)
-----------------------------
After encrypting and writing to DB, provision() immediately opens the
CRM adapter and calls verify_connection(). If the CRM rejects the
credentials the DB row is wiped (revoke wipe=True) and a 502 is returned.
The admin never ends up with an integration row pointing to bad creds.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.core.auth import get_current_user
from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialNotFoundError,
    CredentialSaveError,
)
from app.factory.adapter_factory import CrmAdapterFactory, AdapterFactoryError
from app.schemas.credentials import (
    CredentialStatusResponse,
    ProvisionCredentialsRequest,
    UpdateCredentialsRequest,
)
from app.services.credential_service import CredentialProvisioningService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["Credentials"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def get_credential_manager(
    request: Request,
) -> AsyncInfisicalCredentialManager:
    """
    Returns the AsyncInfisicalCredentialManager (key manager) from app.state.
    Used by CredentialProvisioningService to call get_active_key_and_version().
    Distinct from app.state.credential_service (AsyncDbBackedCredentialService).
    """
    return request.app.state.key_manager


async def get_adapter_factory(request: Request) -> CrmAdapterFactory:
    """Return the CrmAdapterFactory from app.state."""
    factory: CrmAdapterFactory | None = getattr(
        request.app.state, "adapter_factory", None
    )
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM adapter factory is not initialised.",
        )
    return factory


async def get_provisioning_service(
    db: AsyncSession = Depends(get_db),
    key_manager: AsyncInfisicalCredentialManager = Depends(get_credential_manager),
) -> CredentialProvisioningService:
    return CredentialProvisioningService(key_manager=key_manager, db=db)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

async def _verify_adapter_connection(
    integration_id: UUID,
    factory: CrmAdapterFactory,
) -> None:
    """
    Open the adapter (authenticate()) then call verify_connection().

    Raises the underlying exception on failure so the route can translate
    it into the correct HTTPException and roll back if needed.
    """
    try:
        adapter = await factory.create(str(integration_id))
    except AdapterFactoryError as exc:
        raise RuntimeError(
            f"Adapter could not be constructed after provisioning: {exc}"
        ) from exc

    async with adapter:
        await adapter.verify_connection()


# ---------------------------------------------------------------------------
# Exception helper
# ---------------------------------------------------------------------------

def _handle_service_error(exc: Exception, integration_id: UUID | None) -> None:
    """Centralised exception → HTTPException mapping."""
    if isinstance(exc, CredentialNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No integration found with id={integration_id}",
        )
    if isinstance(exc, (CredentialDecodeError, CredentialSaveError)):
        logger.error(
            "Credential operation failed",
            extra={"integration_id": str(integration_id), "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Credential operation failed. Check server logs.",
        )
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    logger.exception(
        "Unexpected error", extra={"integration_id": str(integration_id)}
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected server error.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/",
    response_model=CredentialStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision credentials for a new integration",
    description=(
        "Creates a new CRM integration. The integration_id is generated "
        "server-side and returned in the response. "
        "After storing credentials, the endpoint immediately verifies them "
        "against the live CRM. If the CRM rejects them the row is wiped and "
        "a 502 is returned — no orphaned integration rows are left behind. "
        "Raw secrets are NEVER logged or returned."
    ),
)
async def provision_credentials(
    body: ProvisionCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
) -> CredentialStatusResponse:
    """
    Steps
    -----
    1. Encrypt and write credentials to DB (via CredentialProvisioningService).
    2. Open the CRM adapter and call verify_connection() against the live CRM.
    3a. On success: return 201 with integration metadata.
    3b. On CRM rejection: wipe the DB row and return 502.
    """
    # ── Step 1: Provision (encrypt → DB write) ────────────────────────────
    try:
        result = await service.provision(
            tenant_id=current_user.tenant_id,
            request=body,
        )
    except Exception as exc:
        _handle_service_error(exc, integration_id=None)

    integration_id: UUID = result.integration_id

    # ── Step 2: Verify credentials against the live CRM ──────────────────
    # If this fails we roll back the DB row so the admin is never left with
    # an integration record that points to credentials the CRM rejects.
    try:
        await _verify_adapter_connection(integration_id, factory)
    except Exception as exc:
        logger.warning(
            "CRM rejected credentials for new integration_id='%s'; "
            "wiping DB row. Reason: %s",
            integration_id,
            exc,
        )
        # Best-effort wipe — log but don't mask the original auth error
        try:
            await service.revoke(integration_id=integration_id, wipe=True)
        except Exception as cleanup_exc:
            logger.error(
                "Failed to wipe integration row for integration_id='%s' "
                "after CRM rejection: %s",
                integration_id,
                cleanup_exc,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Credentials were stored but the CRM rejected them: {exc}. "
                "No integration was created. Check your token and base_url."
            ),
        )

    # ── Step 3: Return the already-built status response ──────────────────
    logger.info(
        "Provisioned and verified integration_id='%s' crm_type='%s'.",
        integration_id,
        result.crm_type,
    )
    return result


@router.patch(
    "/{integration_id}/credentials",
    response_model=CredentialStatusResponse,
    summary="Partially update credentials or metadata",
    description=(
        "Only fields present in the request body are updated. "
        "New credentials are re-encrypted with the current active key "
        "and immediately verified against the live CRM."
    ),
)
async def update_credentials(
    integration_id: UUID,
    body: UpdateCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
) -> CredentialStatusResponse:
    try:
        result = await service.update(integration_id=integration_id, request=body)
    except Exception as exc:
        _handle_service_error(exc, integration_id)

    # Only verify if credentials themselves changed — no point hitting the
    # CRM if only base_url metadata was updated without new secrets.
    if body.credentials is not None:
        try:
            await _verify_adapter_connection(integration_id, factory)
        except Exception as exc:
            logger.warning(
                "Updated credentials rejected by CRM for integration_id='%s': %s",
                integration_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Credentials were updated in the store but the CRM "
                    f"rejected them: {exc}. "
                    "Please update again with valid credentials."
                ),
            )

    return result


@router.get(
    "/{integration_id}/credentials/status",
    response_model=CredentialStatusResponse,
    summary="Get credential status (no secrets returned)",
    description="Returns metadata and status for an integration. Credentials are never decrypted.",
)
async def get_credential_status(
    integration_id: UUID,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> CredentialStatusResponse:
    try:
        return await service.get_status(integration_id=integration_id)
    except Exception as exc:
        _handle_service_error(exc, integration_id)


@router.post(
    "/{integration_id}/credentials/rotate",
    summary="Re-encrypt credentials with the current active key version",
    description=(
        "Decrypts the stored credentials with the old key and re-encrypts "
        "with the current active Infisical key. Run this on all active "
        "integrations after a key rotation."
    ),
)
async def rotate_credentials(
    integration_id: UUID,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> dict:
    try:
        return await service.rotate(integration_id=integration_id)
    except Exception as exc:
        _handle_service_error(exc, integration_id)


# @router.delete(
#     "/{integration_id}/credentials",
#     status_code=status.HTTP_204_NO_CONTENT,
#     response_class=Response,
#     summary="Revoke integration credentials",
#     description=(
#         "Soft-disables the integration (is_active=False). "
#         "Pass ?wipe=true to also null out the encrypted credential blob."
#     ),
# )
async def revoke_credentials(
    integration_id: UUID,
    wipe: bool = Query(
        default=False,
        description="If true, nulls out credential_enc (hard wipe).",
    ),
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> None:
    try:
        await service.revoke(integration_id=integration_id, wipe=wipe)
    except Exception as exc:
        _handle_service_error(exc, integration_id)