# app/routes/integrations.py
"""
Integrations Router
===================
Handles the full lifecycle of a CRM integration credential:
  POST   /integrations              — provision a new integration
  GET    /integrations              — list all integrations for the tenant
  GET    /integrations/{id}         — get one integration record
  PATCH  /integrations/{id}/rotate  — rotate credentials
  DELETE /integrations/{id}         — de-provision

These are the ONLY routes that touch Infisical directly.
All other routes (tickets, agents, sync) use the factory, which reads
credentials via the credential manager internally.

DB contract
-----------
Only integration_id (UUID) and crm_type are persisted to PostgreSQL.
All credentials live in Infisical under key CREDS_<integration_id>.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.adapter_dependencies.deps import get_adapter_factory, get_credential_manager
from app.config.registry import AdapterNotFoundError, AdapterRegistry
from app.adapter_dependencies.deps import get_adapter_registry
from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.exceptions import CredentialNotFoundError
from app.credentials.models import CrmCredentialEnvelope
from app.factory.adapter_factory import CrmAdapterFactory

router = APIRouter(prefix="/integrations", tags=["Integrations"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ProvisionIntegrationRequest(BaseModel):
    """
    Body for POST /integrations.

    crm_type must match a key in crm_adapters.yaml (e.g. "zammad", "espocrm").
    base_url is the tenant-specific CRM instance URL.
    credentials must contain a 'strategy' key plus the strategy-specific fields.

    Example (api_token):
        {
            "crm_type": "zammad",
            "base_url": "https://support.acme.com",
            "credentials": {"strategy": "api_token", "token": "abc123"}
        }

    Example (basic):
        {
            "crm_type": "espocrm",
            "base_url": "https://crm.acme.com",
            "credentials": {"strategy": "basic", "username": "u", "password": "p"}
        }
    """
    crm_type: str = Field(..., description="Adapter key from crm_adapters.yaml")
    base_url: str = Field(..., description="Tenant CRM instance URL")
    credentials: Dict[str, Any] = Field(..., description="Auth credentials dict")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RotateCredentialsRequest(BaseModel):
    """Body for PATCH /integrations/{id}/rotate."""
    base_url: str
    credentials: Dict[str, Any]


class IntegrationResponse(BaseModel):
    """Safe response — never includes credentials."""
    integration_id: str
    crm_type: str
    base_url: str
    message: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=IntegrationResponse,
    summary="Provision a new CRM integration",
)
async def provision_integration(
    body: ProvisionIntegrationRequest,
    registry: AdapterRegistry = Depends(get_adapter_registry),
    cred_manager: AsyncInfisicalCredentialManager = Depends(get_credential_manager),
):
    """
    Steps
    -----
    1. Validate that crm_type exists in the adapter registry.
    2. Validate the credential envelope (Pydantic rejects bad strategy/URL).
    3. Generate a new integration_id UUID.
    4. Write credentials to Infisical under CREDS_<integration_id>.
    5. Persist integration_id + crm_type to PostgreSQL.
    6. Return integration_id to the caller — this is the only handle they need.
    """
    # Step 1 — crm_type must be registered
    try:
        registry.get_entry(body.crm_type)
    except AdapterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown CRM type '{body.crm_type}'. "
                   f"Available: {registry.list_adapter_keys()}",
        )

    # Step 2 — build and validate the envelope
    try:
        envelope = CrmCredentialEnvelope(
            crm_type=body.crm_type,
            base_url=body.base_url,
            credentials=body.credentials,
            metadata=body.metadata,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid credentials payload: {exc}",
        )

    # Step 3 — generate integration_id
    integration_id = str(uuid.uuid4())

    # Step 4 — write to Infisical
    try:
        await cred_manager.save_credentials(integration_id, envelope)
    except Exception as exc:
        logger.error("Failed to save credentials for new integration: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store credentials. Try again.",
        )

    # Step 5 — persist to DB
    # TODO: Replace with your IntegrationRepository.create() call.
    # Example:
    #   async with async_session_maker() as db:
    #       await integration_repo.create(db, integration_id=integration_id, crm_type=body.crm_type)
    #       await db.commit()
    logger.info(
        "Provisioned integration_id='%s' for crm_type='%s'.",
        integration_id,
        body.crm_type,
    )

    return IntegrationResponse(
        integration_id=integration_id,
        crm_type=body.crm_type,
        base_url=envelope.base_url,
        message="Integration provisioned successfully.",
    )


@router.patch(
    "/{integration_id}/rotate",
    response_model=IntegrationResponse,
    summary="Rotate credentials for an existing integration",
)
async def rotate_credentials(
    integration_id: str,
    body: RotateCredentialsRequest,
    cred_manager: AsyncInfisicalCredentialManager = Depends(get_credential_manager),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
):
    """
    Replaces the stored credentials in Infisical with the new ones and
    immediately reads them back to confirm the write succeeded.

    The integration_id and crm_type stay unchanged — only the secrets rotate.
    """
    # Fetch the existing envelope to preserve crm_type
    try:
        existing = await cred_manager.get_credentials(integration_id)
    except CredentialNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Integration '{integration_id}' not found.",
        )

    try:
        new_envelope = CrmCredentialEnvelope(
            crm_type=existing.crm_type,
            base_url=body.base_url,
            credentials=body.credentials,
            metadata=existing.metadata,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid credentials: {exc}",
        )

    # rotate_credentials = save + verified read-back
    confirmed = await cred_manager.rotate_credentials(integration_id, new_envelope)

    # Bust the factory's class cache so the next create() re-verifies auth
    factory.clear_class_cache()

    logger.info("Rotated credentials for integration_id='%s'.", integration_id)
    return IntegrationResponse(
        integration_id=integration_id,
        crm_type=confirmed.crm_type,
        base_url=confirmed.base_url,
        message="Credentials rotated successfully.",
    )


@router.delete(
    "/{integration_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="De-provision a CRM integration",
)
async def delete_integration(
    integration_id: str,
    cred_manager: AsyncInfisicalCredentialManager = Depends(get_credential_manager),
):
    """
    Removes the Infisical secret and the DB record for this integration.
    Idempotent — deleting a non-existent integration returns 204.
    """
    # Delete from Infisical (idempotent — no-op if already gone)
    await cred_manager.delete_credentials(integration_id)

    # TODO: Delete from PostgreSQL
    # Example:
    #   async with async_session_maker() as db:
    #       await integration_repo.delete(db, integration_id)
    #       await db.commit()

    logger.info("De-provisioned integration_id='%s'.", integration_id)


@router.get(
    "/{integration_id}/verify",
    summary="Verify credentials are valid by authenticating with the CRM",
)
async def verify_integration(
    integration_id: str,
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
):
    """
    Attempts to open the adapter (authenticate with the CRM) and immediately
    closes it.  Returns 200 if successful, propagates CrmAuthError → 502
    via the exception handler if credentials are rejected.

    Useful as a health check after provisioning or rotation.
    """
    try:
        adapter = factory.create(integration_id)
        async with adapter:
            pass   # authenticate() is called inside open() inside async with
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"CRM authentication failed: {exc}",
        )

    return {"integration_id": integration_id, "status": "verified"}