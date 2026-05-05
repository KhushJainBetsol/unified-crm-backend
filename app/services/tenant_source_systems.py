"""
Service layer — tenant_source_systems existence check + active integrations list
              + full integration detail with decrypted credentials.
"""

from __future__ import annotations

import uuid
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_source_systems import TenantSourceSystem
from app.models.crm_integration import CrmIntegration
from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.db_credential_service import _fetch_key_for_row, _build_credentials_dict
from app.credentials.encryption import EncryptionService
from app.credentials.exceptions import CredentialNotFoundError, CredentialDecodeError
from app.schemas.tenant_source_systems import (
    TenantSourceSystemCheckRequest,
    TenantSourceSystemCheckResponse,
    TenantActiveIntegrationsResponse,
    TenantIntegrationDetailResponse,
)

logger = logging.getLogger(__name__)


async def check_tenant_source_system(
    request: TenantSourceSystemCheckRequest,
    db: AsyncSession,
) -> TenantSourceSystemCheckResponse:
    """
    Check whether a (tenant_id, source_system_id) pair exists in
    tenant_source_systems and return a structured response.
    """
    stmt = select(TenantSourceSystem).where(
        TenantSourceSystem.tenant_id == request.tenant_id,
        TenantSourceSystem.source_system_id == request.source_system_id,
    )
    result = await db.execute(stmt)
    record: TenantSourceSystem | None = result.scalar_one_or_none()

    if record is None:
        return TenantSourceSystemCheckResponse(
            exists=False,
            is_active=None,
            message=(
                f"No mapping found for tenant_id='{request.tenant_id}' "
                f"and source_system_id={request.source_system_id}."
            ),
            tenant_id=request.tenant_id,
            source_system_id=request.source_system_id,
        )

    return TenantSourceSystemCheckResponse(
        exists=True,
        is_active=record.is_active,
        message=(
            f"Mapping exists for tenant_id='{request.tenant_id}' "
            f"and source_system_id={request.source_system_id}. "
            f"Status: {'active' if record.is_active else 'inactive'}."
        ),
        tenant_id=request.tenant_id,
        source_system_id=request.source_system_id,
    )


async def get_active_integrations(
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> TenantActiveIntegrationsResponse:
    """
    Return all source_system_ids where is_active=True for the given tenant.
    """
    stmt = select(TenantSourceSystem.source_system_id).where(
        TenantSourceSystem.tenant_id == tenant_id,
        TenantSourceSystem.is_active.is_(True),
    )
    result = await db.execute(stmt)
    active_ids: list[int] = list(result.scalars().all())

    return TenantActiveIntegrationsResponse(
        tenant_id=tenant_id,
        active_source_system_ids=active_ids,
        count=len(active_ids),
    )


async def get_integration_detail(
    tenant_id: uuid.UUID,
    source_system_id: int,
    db: AsyncSession,
    key_manager: AsyncInfisicalCredentialManager,
) -> TenantIntegrationDetailResponse:
    """
    Full integration detail flow:

    1. Look up tenant_source_systems → get integration_id.
    2. Fetch crm_integrations row for that integration_id.
    3. Fetch the AES key from Infisical (tenant-versioned or global fallback).
    4. Decrypt credential_enc  → parsed credentials dict.
    5. Decrypt webhook_secrets_enc (if present) → dict.
    6. Return a structured JSON-serialisable response.

    Raises
    ------
    CredentialNotFoundError
        If no mapping or no active integration row exists.
    CredentialDecodeError
        If key fetch, AES decrypt, or JSON parse fails.
    """
    # ── 1. Resolve integration_id via tenant_source_systems ──────────────
    tss_stmt = select(TenantSourceSystem).where(
        TenantSourceSystem.tenant_id == tenant_id,
        TenantSourceSystem.source_system_id == source_system_id,
    )
    tss_result = await db.execute(tss_stmt)
    tss_record: TenantSourceSystem | None = tss_result.scalar_one_or_none()

    if tss_record is None:
        raise CredentialNotFoundError(
            f"No mapping found for tenant_id='{tenant_id}' "
            f"and source_system_id={source_system_id}."
        )

    integration_id = str(tss_record.integration_id)

    # ── 2. Fetch crm_integrations row ─────────────────────────────────────
    intg_stmt = select(CrmIntegration).where(
        CrmIntegration.id == integration_id,
        CrmIntegration.is_active.is_(True),
    )
    intg_result = await db.execute(intg_stmt)
    row: CrmIntegration | None = intg_result.scalar_one_or_none()

    if row is None:
        raise CredentialNotFoundError(
            f"No active crm_integration found for integration_id='{integration_id}'."
        )

    if not row.credential_enc:
        raise CredentialDecodeError(
            integration_id,
            "credential_enc column is empty — integration was never provisioned.",
        )

    # ── 3. Fetch AES key from Infisical ───────────────────────────────────
    # Try per-tenant versioned key first; fall back to global key.
    try:
        raw_key = await key_manager.get_tenant_key(str(row.tenant_id), row.key_version)
        if raw_key is None:
            # Fall back to global encryption key for this version
            raw_key = await key_manager.get_encryption_key(row.key_version)
    except Exception as exc:
        raise CredentialDecodeError(
            integration_id,
            f"Infisical key fetch failed for version='{row.key_version}': {exc}",
        ) from exc

    enc_service = EncryptionService(raw_key=raw_key, key_version=row.key_version)

    # ── 4. Decrypt credential_enc ─────────────────────────────────────────
    try:
        decrypted_cred_json = enc_service.decrypt_from_db(row.credential_enc)
    except Exception as exc:
        raise CredentialDecodeError(
            integration_id,
            f"AES decryption of credential_enc failed: {exc}",
        ) from exc

    credentials_dict = _build_credentials_dict(row.auth_type, decrypted_cred_json)

    # ── 5. Decrypt webhook_secrets_enc (optional) ─────────────────────────
    webhook_secrets: dict | None = None
    if row.webhook_secrets_enc:
        try:
            webhook_secrets = enc_service.decrypt_dict_from_db(row.webhook_secrets_enc)
        except Exception as exc:
            # Non-fatal: log and continue without webhook secrets
            logger.warning(
                "Failed to decrypt webhook_secrets_enc for integration_id='%s': %s",
                integration_id,
                exc,
            )

    # ── 6. Build response ─────────────────────────────────────────────────
    crm_type = (
        row.source_system.system_name
        if row.source_system
        else "unknown"
    )

    logger.info(
        "get_integration_detail: resolved integration_id='%s' "
        "(tenant_id=%s, source_system_id=%s, crm_type=%s).",
        integration_id,
        tenant_id,
        source_system_id,
        crm_type,
    )

    return TenantIntegrationDetailResponse(
        integration_id=integration_id,
        tenant_id=str(tenant_id),
        source_system_id=source_system_id,
        crm_type=crm_type,
        auth_type=row.auth_type,
        key_version=row.key_version,
        base_url=row.base_url or "",
        webhook_uuid=str(row.webhook_uuid) if row.webhook_uuid else None,
        is_active=row.is_active,
        credentials=credentials_dict,
        webhook_secrets=webhook_secrets,
        token_expires_at=row.token_expires_at.isoformat() if row.token_expires_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )