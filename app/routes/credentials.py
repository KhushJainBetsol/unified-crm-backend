"""
app/routes/credentials.py

REST API for CRM credential lifecycle.

Endpoints
---------
POST   /api/v1/integrations/                               → provision (ID auto-generated)
PATCH  /api/v1/integrations/{integration_id}/credentials   → partial update
GET    /api/v1/integrations/{integration_id}/credentials/status
POST   /api/v1/integrations/{integration_id}/credentials/rotate
DELETE /api/v1/integrations/{integration_id}/credentials   → revoke

Auth
----
All write endpoints require a valid Keycloak JWT.
tenant_id is extracted from the JWT claims — callers never pass it explicitly.
Secrets are NEVER returned in any response.
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
    Returns the AsyncInfisicalCredentialManager attached to app.state
    during lifespan startup in main.py.

    main.py must store the key manager explicitly:

        app.state.key_manager = AsyncInfisicalCredentialManager(...)

    This is separate from app.state.credential_service
    (AsyncDbBackedCredentialService), which is used by the adapter factory.
    CredentialProvisioningService needs the raw key manager to call
    get_active_key_and_version() / get_encryption_key() directly.
    """
    return request.app.state.key_manager


async def get_provisioning_service(
    db: AsyncSession = Depends(get_db),
    key_manager: AsyncInfisicalCredentialManager = Depends(get_credential_manager),
) -> CredentialProvisioningService:
    return CredentialProvisioningService(key_manager=key_manager, db=db)


# ---------------------------------------------------------------------------
# Helper: translate service exceptions → HTTP responses
# ---------------------------------------------------------------------------


def _handle_service_error(exc: Exception, integration_id: UUID | None) -> None:
    """
    Centralised exception → HTTPException mapping.
    Keeps route handlers free of error-handling boilerplate.
    """
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
        "Unexpected error",
        extra={"integration_id": str(integration_id)},
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
        "server-side and returned in the response — callers must NOT supply it. "
        "Credentials are AES-256-CBC encrypted with the current active Infisical key. "
        "Raw secrets are NEVER logged or returned."
    ),
)
async def provision_credentials(
    body: ProvisionCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> CredentialStatusResponse:
    """tenant_id comes exclusively from the verified JWT — never from the request body."""
    try:
        return await service.provision(
            tenant_id=current_user.tenant_id,
            request=body,
        )
    except Exception as exc:
        _handle_service_error(exc, integration_id=None)


@router.patch(
    "/{integration_id}/credentials",
    response_model=CredentialStatusResponse,
    summary="Partially update credentials or metadata",
    description=(
        "Only the fields present in the request body are updated. "
        "If new credentials are provided they are re-encrypted with the current active key."
    ),
)
async def update_credentials(
    integration_id: UUID,
    body: UpdateCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> CredentialStatusResponse:
    try:
        return await service.update(integration_id=integration_id, request=body)
    except Exception as exc:
        _handle_service_error(exc, integration_id)


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
        "Decrypts the stored credentials with the old key and re-encrypts with "
        "the current active Infisical key. Run this on all active integrations "
        "after a key rotation."
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
# async def revoke_credentials(
#     integration_id: UUID,
#     wipe: bool = Query(
#         default=False,
#         description="If true, nulls out credential_enc (hard wipe). Otherwise soft-disables only.",
#     ),
#     current_user=Depends(get_current_user),
#     service: CredentialProvisioningService = Depends(get_provisioning_service),
# ) -> None:
#     try:
#         await service.revoke(integration_id=integration_id, wipe=wipe)
#     except Exception as exc:
#         _handle_service_error(exc, integration_id)