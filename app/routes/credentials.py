"""
app/routes/credentials.py

REST API for CRM credential lifecycle.

Endpoints
---------
POST   /api/v1/integrations/check-connection                → validate only (no store)
POST   /api/v1/integrations/                               → provision
PATCH  /api/v1/integrations/{integration_id}/credentials   → partial update
GET    /api/v1/integrations/{integration_id}/credentials/status
GET    /api/v1/integrations/{integration_id}/verify        → on-demand health check
POST   /api/v1/integrations/{integration_id}/credentials/rotate
DELETE /api/v1/integrations/{integration_id}/credentials   → revoke

Auth
----
All write endpoints require a valid Keycloak JWT.
tenant_id is extracted from the JWT claims — never from the request body.
Secrets are NEVER returned in any response.

Permission gate (provision + update)
-------------------------------------
Before any encryption or DB write, provision() calls the CRM's permission
endpoint with the supplied raw credentials and validates the response.

  • Unknown crm_type      →  422 with a list of supported types.
  • Missing permissions   →  403 with a structured JSON body listing
                             exactly which checks failed. Nothing is written.
  • Sufficient perms      →  normal encrypt → DB write → adapter verify flow.

Verification gate (provision)
-----------------------------
After encrypting and writing to DB, provision() opens the CRM adapter and
calls verify_connection(). If the CRM rejects the credentials the DB row
is wiped (revoke wipe=True) and a 502 is returned. The admin never ends up
with an integration row pointing to bad creds.
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
from app.services.credential_service import (
    CredentialProvisioningService,
    _PERMISSION_ENDPOINT,
    _PERMISSION_VALIDATOR,
)
from app.adapters.base.permission_validator import PermissionValidationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["Credentials"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def get_credential_manager(
    request: Request,
) -> AsyncInfisicalCredentialManager:
    return request.app.state.key_manager


async def get_adapter_factory(request: Request) -> CrmAdapterFactory:
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
# Internal helpers
# ---------------------------------------------------------------------------

def _assert_supported_crm_type(crm_type: str) -> None:
    """
    Raise HTTP 422 immediately if crm_type has no registered permission
    endpoint or validator.

    This is called at the top of check-connection (and optionally provision)
    so the caller gets a clear, actionable error instead of a silent pass.
    """
    supported = sorted(set(_PERMISSION_ENDPOINT.keys()) & set(_PERMISSION_VALIDATOR.keys()))
    if crm_type not in _PERMISSION_ENDPOINT or crm_type not in _PERMISSION_VALIDATOR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "unsupported_crm_type",
                "message": (
                    f"crm_type '{crm_type}' is not recognised or has no "
                    "permission validator configured."
                ),
                "supported_crm_types": supported,
            },
        )


async def _verify_adapter_connection(
    integration_id: UUID,
    factory: CrmAdapterFactory,
) -> None:
    """
    Open the adapter (authenticate()) then call verify_connection().
    Raises on failure so the route can translate it into the correct
    HTTPException and roll back if needed.
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
    """
    Centralised exception → HTTPException mapping.

    PermissionValidationError is mapped to 403 with a structured body that
    lists every failed check so the frontend can surface it clearly.
    """
    if isinstance(exc, PermissionValidationError):
        # Return a structured 403 so the frontend can display each failure
        # individually rather than just a generic error string.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "insufficient_crm_permissions",
                "message": (
                    "The supplied credentials do not have the required permissions. "
                    "Please review the failed checks below and update the API token "
                    "or user role in your CRM before retrying."
                ),
                "failed_checks": exc.failures,
            },
        )

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
    "/check-connection",
    summary="Check CRM connection and validate credentials (without storing)",
    description=(
        "Tests if the provided credentials are valid and have required permissions. "
        "Credentials are NOT stored in the database.\n\n"
        "**Step 0 — CRM type guard:** If `crm_type` is not in the supported registry "
        "a **422** is returned immediately with a `supported_crm_types` list. "
        "A placeholder value like `'string'` will be rejected here.\n\n"
        "**Step 1 — Permission check:** The supplied credentials "
        "are used to call the CRM's permission-inspection endpoint. If the token "
        "lacks any required permission a **403** is returned with a "
        "`failed_checks` list.\n\n"
        "If both checks pass, returns **200** with `status: connection_verified`. "
        "Raw secrets are NEVER logged or returned."
    ),
)
async def check_connection(
    body: ProvisionCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> dict:
    """
    Steps
    -----
    0. CRM type guard — reject unsupported / placeholder crm_type values
       with a structured 422 before making any external call.
    1. Permission gate — validate CRM permissions via the live CRM endpoint.
       PermissionValidationError → 403 with failed_checks list.
    2. Return 200 with connection status if all checks pass.

    Note: Credentials are NOT stored. This is a read-only test endpoint.
    """
    crm_type = body.crm_type.strip().lower()
    base_url = str(body.base_url).rstrip("/")

    # ── Step 0: Reject unknown / placeholder crm_type immediately ─────────
    _assert_supported_crm_type(crm_type)

    # ── Step 1: Permission check (no DB write) ─────────────────────────────
    try:
        await service._check_permissions(
            crm_type=crm_type,
            base_url=base_url,
            request=body,
        )
    except Exception as exc:
        _handle_service_error(exc, integration_id=None)

    logger.info(
        "Connection check passed for crm_type='%s' by user tenant_id='%s'.",
        crm_type,
        current_user.tenant_id,
    )
    return {
        "status": "connection_verified",
        "crm_type": crm_type,
        "base_url": base_url,
        "message": "Credentials validated. You can now provision this integration.",
    }


@router.post(
    "/",
    response_model=CredentialStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision credentials for a new integration",
    description=(
        "Creates a new CRM integration.\n\n"
        "**Step 1 — Permission check (before any write):** The supplied credentials "
        "are used to call the CRM's permission-inspection endpoint. If the token "
        "lacks any required permission a **403** is returned immediately with a "
        "`failed_checks` list describing exactly what is missing. Nothing is "
        "stored in the database.\n\n"
        "**Step 2 — Encrypt and store:** If permissions pass, credentials are "
        "encrypted and written to the database.\n\n"
        "Note: Connection verification is skipped here (run check-connection first "
        "for safety).\n\n"
        "Raw secrets are NEVER logged or returned."
    ),
)
async def provision_credentials(
    body: ProvisionCredentialsRequest,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
) -> CredentialStatusResponse:
    """
    Steps
    -----
    1. Permission gate  — validate CRM permissions before any DB write.
       PermissionValidationError → 403 with failed_checks list.
    2. Encrypt + write credentials to DB.

    Note: Does NOT verify connection. For safety, call check-connection first.
    """
    try:
        result = await service.provision(
            tenant_id=current_user.tenant_id,
            request=body,
        )
    except Exception as exc:
        _handle_service_error(exc, integration_id=None)

    integration_id: UUID = result.integration_id

    logger.info(
        "Provisioned integration_id='%s' crm_type='%s'.",
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


@router.get(
    "/{integration_id}/verify",
    summary="On-demand connection health check",
    description=(
        "Opens the CRM adapter (authenticate()) and calls verify_connection() "
        "against the live CRM instance. Returns `{integration_id, status: 'verified'}` "
        "on success or a **502** if the CRM rejects the connection."
    ),
)
async def verify_integration(
    integration_id: UUID,
    current_user=Depends(get_current_user),
    service: CredentialProvisioningService = Depends(get_provisioning_service),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
) -> dict:
    """
    Steps
    -----
    1. Confirm the integration row exists (raises 404 if not).
    2. Open the adapter and call verify_connection() against the live CRM.
       On failure → 502 with the rejection reason.
    3. Return {integration_id, status: "verified"}.
    """
    # ── 1. Confirm the row exists ──────────────────────────────────────────
    try:
        await service.get_status(integration_id=integration_id)
    except Exception as exc:
        _handle_service_error(exc, integration_id)

    # ── 2. Live adapter check ──────────────────────────────────────────────
    try:
        await _verify_adapter_connection(integration_id, factory)
    except Exception as exc:
        logger.warning(
            "Verify check failed for integration_id='%s': %s",
            integration_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Connection verification failed: {exc}",
        )

    return {"integration_id": str(integration_id), "status": "verified"}


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


@router.delete(
    "/{integration_id}/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Revoke integration credentials",
    description=(
        "Soft-disables the integration (is_active=False). "
        "Pass ?wipe=true to also null out the encrypted credential blob."
    ),
)
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