"""
app/services/credential_service.py

CredentialProvisioningService
==============================
Orchestrates the full lifecycle of CRM credentials:
  PROVISION  — upsert: if tenant already has this CRM → update, else → create
  UPDATE     — partial update of credentials / metadata / webhook secrets
  RETRIEVE   — DB lookup → Infisical key fetch → AES decrypt → envelope
  ROTATE     — re-encrypt BOTH _enc columns with current active key
  REVOKE     — soft-delete (is_active=False) or wipe both _enc columns
               + cascade soft-delete all Ticket/Agent/Customer/Company rows
                 owned by the disconnected (tenant_id, source_system_id)

Two-column secret model
-----------------------
  credential_enc      → outbound auth secrets (api token, password, OAuth tokens…)
  webhook_secrets_enc → inbound webhook HMAC secrets (nullable — always may be None)

Key versioning (v3)
--------------------
``crm_integrations.key_version`` stores the REAL version tag returned by
Infisical (e.g. "v1", "v2") — never the opaque literal "tenant".

Lookup table:
  key_version == "v1"  →  TENANT_KEY_<tenant_id>_v1  (per-tenant versioned key)
  key_version == "v2"  →  TENANT_KEY_<tenant_id>_v2  (per-tenant versioned key)
  ...

If no per-tenant key exists for the stored version, the service falls back to
the global ENCRYPTION_KEY_<version> secret (legacy tenants created before
the per-tenant key rollout).

The rotation scheduler updates key_version on each row after re-encryption
so this lookup always resolves correctly.

Permission gate (PROVISION + CHECK-CONNECTION)
----------------------------------------------
Before any encryption or DB write, provision() calls _check_permissions()
which hits the CRM's permission-inspection endpoint.

Cascade soft-delete (REVOKE)
-----------------------------
When an integration is revoked, all Ticket, Agent, Customer, and Company
rows belonging to (tenant_id, source_system_id) are bulk soft-deleted in
the same transaction.

Ticket rows require special handling: the ck_ticket_deletion_source CHECK
constraint requires that is_deleted=TRUE be accompanied by either
deleted_by_id IS NOT NULL (dashboard deletion) or is_deleted_by_crm=TRUE
(CRM-side deletion). Integration disconnect sets is_deleted_by_crm=True.

Cascade restore (PROVISION upsert)
------------------------------------
When the same CRM is re-provisioned for a tenant that previously disconnected
it, all soft-deleted rows are restored before the fresh sync runs.
Ticket rows also have is_deleted_by_crm reset to False on restore.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.encryption import EncryptionService
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialNotFoundError,
)
from app.credentials.models import CrmCredentialEnvelope
from app.models.agent import Agent
from app.models.crm_integration import CrmIntegration
from app.models.customer import Customer
from app.models.ticket import Ticket
from app.models.tenant_source_systems import TenantSourceSystem
from app.models.tenant import Tenant
from app.models.source_system import SourceSystem
from app.schemas.credentials import (
    CredentialStatusResponse,
    ProvisionCredentialsRequest,
    UpdateCredentialsRequest,
)
from app.adapters.base.permission_validator import (
    EspoCrmPermissionValidator,
    PermissionValidationError,
    ZammadPermissionValidator,
)

logger = logging.getLogger(__name__)

_PERMISSION_ENDPOINT: Dict[str, str] = {
    "espocrm": "/api/v1/App/user",
    "zammad":  "/api/v1/user_access_token",
}

_PERMISSION_VALIDATOR = {
    "espocrm": EspoCrmPermissionValidator,
    "zammad":  ZammadPermissionValidator,
}

# Models that participate in the generic cascade soft-delete / restore cycle.
# Ticket is handled separately due to the ck_ticket_deletion_source CHECK
# constraint that requires is_deleted_by_crm=True when deleted via integration.
_CASCADE_MODELS = (Agent, Customer)


class CredentialProvisioningService:
    """
    Async service for the full credential lifecycle.

    Parameters
    ----------
    key_manager:
        Async Infisical manager.
    db:
        SQLAlchemy AsyncSession scoped to the current request.
    """

    def __init__(
        self,
        key_manager: AsyncInfisicalCredentialManager,
        db: AsyncSession,
    ) -> None:
        self._key_manager = key_manager
        self._db = db

    # ── PROVISION (upsert) ────────────────────────────────────────────────

    async def provision(
        self,
        tenant_id: UUID,
        request: ProvisionCredentialsRequest,
    ) -> CredentialStatusResponse:
        """
        Upsert credentials for a CRM integration.

        Steps
        -----
        1. Fetch tenant row — raises ValueError if not found.
        2. Resolve SourceSystem by crm_type name.
        3. Check for an existing TenantSourceSystem row (upsert guard).
           If found → permission check → cascade restore → update path.
        4. Permission gate — validate CRM permissions before any DB write.
        5. Fetch AES key (versioned per-tenant or global fallback).
        6. Encrypt outbound credentials.
        7. Encrypt inbound webhook secrets (nullable).
        8. Insert CrmIntegration + TenantSourceSystem rows.
        9. Commit and trigger background full sync.
        """
        crm_type = request.crm_type.strip().lower()
        base_url = str(request.base_url).rstrip("/")

        # ── 1. Fetch tenant ────────────────────────────────────────────────
        tenant_result = await self._db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = tenant_result.scalars().first()
        if tenant is None:
            raise ValueError(f"Tenant with id={tenant_id} not found.")

        # ── 2. Resolve SourceSystem ────────────────────────────────────────
        ss_result = await self._db.execute(
            select(SourceSystem).where(SourceSystem.system_name == crm_type)
        )
        source_system = ss_result.scalars().first()
        if source_system is None:
            raise ValueError(
                f"Unknown crm_type '{crm_type}'. "
                "Register it in the source_systems table before provisioning."
            )

        # ── 3. Upsert check ────────────────────────────────────────────────
        tss_result = await self._db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.tenant_id == tenant_id,
                TenantSourceSystem.source_system_id == source_system.id,
            )
        )
        existing_tss = tss_result.scalars().first()

        if existing_tss is not None:
            logger.info(
                "Integration already exists for tenant_id='%s' crm_type='%s' "
                "(integration_id='%s'). Running upsert → update path.",
                tenant_id, crm_type, existing_tss.integration_id,
            )
            await self._check_permissions(crm_type=crm_type, base_url=base_url, request=request)

            # Restore any rows that were soft-deleted when this integration
            # was previously disconnected so the fresh sync re-populates
            # on top of restored rows rather than creating duplicates.
            await self._cascade_restore(
                tenant_id=tenant_id,
                source_system_id=existing_tss.source_system_id,
            )

            update_request = UpdateCredentialsRequest(
                credentials=request.credentials,
                base_url=request.base_url,
            )
            for attr in ("webhook_secret", "webhook_signing_secret", "per_event_secrets"):
                if hasattr(request, attr) and hasattr(update_request, attr):
                    setattr(update_request, attr, getattr(request, attr))

            return await self.update(
                integration_id=existing_tss.integration_id,
                request=update_request,
            )

        # ── 4. Permission gate ─────────────────────────────────────────────
        await self._check_permissions(crm_type=crm_type, base_url=base_url, request=request)

        integration_id = uuid4()

        # ── 5. Fetch AES key (versioned per-tenant or global fallback) ──────
        version, raw_key = await self._get_key_for_tenant(tenant_id)
        enc_service = EncryptionService(raw_key=raw_key, key_version=version)

        # ── 6. Encrypt outbound credentials ───────────────────────────────
        secret_dict = request.credentials.to_secret_dict()
        credential_enc = enc_service.encrypt(json.dumps(secret_dict)).to_db_string()

        # ── 7. Encrypt inbound webhook secrets (nullable) ──────────────────
        webhook_secret_dict = request.build_webhook_secret_dict()
        webhook_secrets_enc: str | None = None
        if webhook_secret_dict:
            webhook_secrets_enc = enc_service.encrypt(
                json.dumps(webhook_secret_dict)
            ).to_db_string()

        auth_type = request.credentials.auth_type

        # ── 8. Insert CrmIntegration row ───────────────────────────────────
        row = await self._create_row(
            integration_id=integration_id,
            tenant_id=tenant_id,
            crm_type=crm_type,
            auth_type=auth_type,
            key_version=version,
            base_url=base_url,
            credential_enc=credential_enc,
            webhook_secrets_enc=webhook_secrets_enc,
            source_system=source_system,
        )

        tss_row = TenantSourceSystem(
            tenant_id=tenant_id,
            source_system_id=source_system.id,
            integration_id=integration_id,
            is_active=True,
        )
        self._db.add(tss_row)

        await self._db.commit()
        await self._db.refresh(row)

        logger.info(
            "Provisioned credentials",
            extra={
                "integration_id": str(integration_id),
                "tenant_id": str(tenant_id),
                "crm_type": crm_type,
                "auth_type": auth_type,
                "key_version": version,
                "has_webhook_secrets": webhook_secrets_enc is not None,
            },
        )

        # ── 9. Trigger background full sync ────────────────────────────────
        import asyncio
        from app.services import scheduler as _scheduler

        asyncio.ensure_future(
            _scheduler.run_tenant_full_sync(tenant_id=tenant_id)
        )
        logger.info(
            "Triggered background sync for newly provisioned tenant_id='%s' crm_type='%s'.",
            tenant_id,
            crm_type,
        )

        return self._to_status(row)

    # ── UPDATE (partial) ───────────────────────────────────────────────────

    async def update(
        self,
        integration_id: UUID,
        request: UpdateCredentialsRequest,
    ) -> CredentialStatusResponse:
        """
        Partially update credentials and/or metadata for an existing integration.

        Only fields present in the request body are written. New credentials
        are re-encrypted with the current active key and the row's key_version
        is updated accordingly.
        """
        row = await self._get_row_or_raise(integration_id)

        if request.credentials is not None:
            version, raw_key = await self._get_key_for_tenant(row.tenant_id)
            enc_service = EncryptionService(raw_key=raw_key, key_version=version)

            secret_dict = request.credentials.to_secret_dict()
            row.credential_enc = enc_service.encrypt(json.dumps(secret_dict)).to_db_string()
            row.auth_type = request.credentials.auth_type
            row.key_version = version

            row.is_active = True

            cred_ws = (
                request.credentials.to_webhook_secret_dict()
                if hasattr(request.credentials, "to_webhook_secret_dict")
                else None
            )
            if cred_ws:
                row.webhook_secrets_enc = enc_service.encrypt(
                    json.dumps(cred_ws)
                ).to_db_string()

        if request.has_webhook_updates():
            ws_version, ws_raw_key = await self._get_key_for_row(row)
            ws_enc = EncryptionService(raw_key=ws_raw_key, key_version=ws_version)

            ws_dict = request.build_webhook_secret_dict()
            if ws_dict:
                row.webhook_secrets_enc = ws_enc.encrypt(json.dumps(ws_dict)).to_db_string()
            else:
                row.webhook_secrets_enc = None

        if request.base_url is not None:
            row.base_url = str(request.base_url).rstrip("/")

        await self._db.commit()
        await self._db.refresh(row)

        logger.info(
            "Partially updated credentials",
            extra={"integration_id": str(integration_id)},
        )
        return self._to_status(row)

    # ── RETRIEVE (decrypt → envelope) ─────────────────────────────────────

    async def get_envelope(self, integration_id: UUID) -> CrmCredentialEnvelope:
        """
        Decrypt stored credentials and return a typed envelope for adapter use.
        Secrets are never surfaced through API responses — only through this
        internal method consumed by the adapter layer.
        """
        row = await self._get_row_or_raise(integration_id)

        if not row.credential_enc:
            raise CredentialNotFoundError(str(integration_id))

        key_version, raw_key = await self._get_key_for_row(row)
        enc_service = EncryptionService(raw_key=raw_key, key_version=key_version)

        try:
            decrypted_json = enc_service.decrypt_from_db(row.credential_enc)
            secret_dict: Dict[str, Any] = json.loads(decrypted_json)
        except Exception as exc:
            raise CredentialDecodeError(str(integration_id), str(exc)) from exc

        webhook_secrets: Dict[str, Any] = {}
        if row.webhook_secrets_enc:
            try:
                webhook_secrets = json.loads(
                    enc_service.decrypt_from_db(row.webhook_secrets_enc)
                )
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt webhook_secrets_enc for integration %s: %s",
                    integration_id, exc,
                )

        crm_type = row.source_system.system_name
        auth_type = row.auth_type
        base_url = row.base_url or ""
        credentials_dict = _secret_dict_to_envelope_creds(auth_type, secret_dict)

        return CrmCredentialEnvelope(
            crm_type=crm_type,
            base_url=base_url,
            credentials=credentials_dict,
            metadata={
                "key_version": key_version,
                "auth_type": auth_type,
                "webhook_secrets": webhook_secrets,
            },
        )

    # ── STATUS ─────────────────────────────────────────────────────────────

    async def get_status(self, integration_id: UUID) -> CredentialStatusResponse:
        row = await self._get_row_or_raise(integration_id)
        return self._to_status(row)

    # ── ROTATE ────────────────────────────────────────────────────────────

    async def rotate(self, integration_id: UUID) -> dict:
        """
        Re-encrypt BOTH _enc columns with the current active key for this tenant.
        Updates key_version on the row to the new version.

        Safe to call even if key_version hasn't changed — decrypt with old key,
        re-encrypt with new key, update the version tag atomically.
        """
        row = await self._get_row_or_raise(integration_id)

        if not row.credential_enc:
            raise CredentialNotFoundError(str(integration_id))

        old_version, old_raw_key = await self._get_key_for_row(row)
        old_enc = EncryptionService(raw_key=old_raw_key, key_version=old_version)

        decrypted_cred = old_enc.decrypt_from_db(row.credential_enc)

        decrypted_webhook: str | None = None
        if row.webhook_secrets_enc:
            decrypted_webhook = old_enc.decrypt_from_db(row.webhook_secrets_enc)

        # Fetch the CURRENT active key (may differ from what's on the row)
        new_version, new_raw_key = await self._get_key_for_tenant(row.tenant_id)
        new_enc = EncryptionService(raw_key=new_raw_key, key_version=new_version)

        row.credential_enc = new_enc.encrypt(decrypted_cred).to_db_string()
        if decrypted_webhook is not None:
            row.webhook_secrets_enc = new_enc.encrypt(decrypted_webhook).to_db_string()

        row.key_version = new_version

        await self._db.commit()

        logger.info(
            "Rotated key",
            extra={
                "integration_id": str(integration_id),
                "old_key_version": old_version,
                "new_key_version": new_version,
                "updated_webhook_secrets": decrypted_webhook is not None,
            },
        )
        return {
            "integration_id": str(integration_id),
            "old_key_version": old_version,
            "new_key_version": new_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── REVOKE ─────────────────────────────────────────────────────────────

    async def revoke(self, integration_id: UUID, *, wipe: bool = False) -> None:
        """
        Soft-disable the integration and cascade soft-delete all data that
        belongs to it.

        Steps
        -----
        1. Mark the integration row as is_active=False.
        2. Optionally wipe encrypted credential blobs (wipe=True).
        3. Delete the TenantSourceSystem mapping row.
        4. Bulk soft-delete all Ticket, Agent, Customer, Company rows
           matching (tenant_id, source_system_id) — same transaction.
           Tickets additionally get is_deleted_by_crm=True to satisfy the
           ck_ticket_deletion_source CHECK constraint.
        5. Commit atomically.
        """
        row = await self._get_row_or_raise(integration_id)

        # Capture before any mutation so the cascade uses the correct values.
        source_system_id: int = row.source_system_id
        tenant_id: UUID = row.tenant_id

        # ── 1. Disable integration ─────────────────────────────────────────
        row.is_active = False

        # ── 2. Optional credential wipe ────────────────────────────────────
        if wipe:
            row.credential_enc = None
            row.webhook_secrets_enc = None
            logger.warning(
                "Wiped credential_enc and webhook_secrets_enc",
                extra={"integration_id": str(integration_id)},
            )

        # ── 3. Remove TenantSourceSystem mapping ───────────────────────────
        tss_result = await self._db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.integration_id == integration_id
            )
        )
        tss_row = tss_result.scalars().first()
        if tss_row is not None:
            await self._db.delete(tss_row)
        else:
            logger.warning(
                "No TenantSourceSystem row found for integration_id='%s'.",
                str(integration_id),
            )

        # ── 4. Cascade soft-delete all owned data ──────────────────────────
        await self._cascade_soft_delete(
            tenant_id=tenant_id,
            source_system_id=source_system_id,
        )

        # ── 5. Commit atomically ───────────────────────────────────────────
        await self._db.commit()

        logger.info(
            "Revoked integration",
            extra={"integration_id": str(integration_id), "wipe": wipe},
        )

    # ── Private helpers ────────────────────────────────────────────────────

    async def _cascade_soft_delete(
        self,
        tenant_id: UUID,
        source_system_id: int,
    ) -> None:
        """
        Bulk soft-delete every Ticket, Agent, Customer, and Company row
        that belongs to the given (tenant_id, source_system_id) pair.

        Ticket rows are handled separately from the other models because of
        the ck_ticket_deletion_source CHECK constraint on the tickets table:

            (is_deleted = TRUE AND is_deleted_by_crm = TRUE  AND deleted_by_id IS NULL)
            OR
            (is_deleted = TRUE AND is_deleted_by_crm = FALSE AND deleted_by_id IS NOT NULL)

        Integration disconnect is a CRM-side event so we set
        is_deleted_by_crm=True and leave deleted_by_id=NULL, which satisfies
        the second branch of the constraint.

        Agent, Customer, Company have no such constraint and are updated with
        a simple is_deleted=True / deleted_at=now() pair.

        Runs inside the caller's transaction — do not commit here.
        Only rows where is_deleted=False are touched so repeated calls
        are idempotent and deleted_at is not overwritten unnecessarily.
        """
        now = datetime.now(timezone.utc)

        # ── Tickets — must satisfy ck_ticket_deletion_source ──────────────
        ticket_result = await self._db.execute(
            update(Ticket)
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.source_system_id == source_system_id,
                Ticket.is_deleted == False,  # noqa: E712
            )
            .values(
                is_deleted=True,
                deleted_at=now,
                is_deleted_by_crm=True,
            )
            .execution_options(synchronize_session="fetch")
        )
        logger.info(
            "Soft-deleted %s rows in 'tickets' for tenant_id='%s' source_system_id=%s",
            ticket_result.rowcount,
            tenant_id,
            source_system_id,
        )

        # ── Agent, Customer, Company — no deletion-source constraint ───────
        for Model in _CASCADE_MODELS:
            result = await self._db.execute(
                update(Model)
                .where(
                    Model.tenant_id == tenant_id,
                    Model.source_system_id == source_system_id,
                    Model.is_deleted == False,  # noqa: E712
                )
                .values(is_deleted=True, deleted_at=now)
                .execution_options(synchronize_session="fetch")
            )
            logger.info(
                "Soft-deleted %s rows in '%s' for tenant_id='%s' source_system_id=%s",
                result.rowcount,
                Model.__tablename__,
                tenant_id,
                source_system_id,
            )

    async def _cascade_restore(
        self,
        tenant_id: UUID,
        source_system_id: int,
    ) -> None:
        """
        Reverse a previous cascade soft-delete when the same integration is
        re-provisioned.

        Sets is_deleted=False / deleted_at=NULL on all rows that match
        (tenant_id, source_system_id) and are currently soft-deleted.

        Ticket rows additionally have is_deleted_by_crm reset to False so
        restored tickets don't appear as CRM-deleted after reconnect.

        The subsequent full sync will overwrite any stale field values so
        restored rows will be current after the sync completes.

        Runs inside the caller's transaction — do not commit here.
        """
        # ── Tickets — also reset is_deleted_by_crm ────────────────────────
        ticket_result = await self._db.execute(
            update(Ticket)
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.source_system_id == source_system_id,
                Ticket.is_deleted == True,  # noqa: E712
            )
            .values(
                is_deleted=False,
                deleted_at=None,
                is_deleted_by_crm=False,
            )
            .execution_options(synchronize_session="fetch")
        )
        logger.info(
            "Restored %s rows in 'tickets' for tenant_id='%s' source_system_id=%s",
            ticket_result.rowcount,
            tenant_id,
            source_system_id,
        )

        # ── Agent, Customer, Company ──────────────────────────────────────
        for Model in _CASCADE_MODELS:
            result = await self._db.execute(
                update(Model)
                .where(
                    Model.tenant_id == tenant_id,
                    Model.source_system_id == source_system_id,
                    Model.is_deleted == True,  # noqa: E712
                )
                .values(is_deleted=False, deleted_at=None)
                .execution_options(synchronize_session="fetch")
            )
            logger.info(
                "Restored %s rows in '%s' for tenant_id='%s' source_system_id=%s",
                result.rowcount,
                Model.__tablename__,
                tenant_id,
                source_system_id,
            )

    async def _check_permissions(
        self,
        crm_type: str,
        base_url: str,
        request: ProvisionCredentialsRequest,
    ) -> None:
        """
        Hit the CRM's permission-inspection endpoint with the supplied raw
        credentials and validate the response.

        Raises
        ------
        ValueError
            If crm_type has no registered endpoint or validator.
        PermissionValidationError
            If the token is rejected (401/403) or lacks required permissions.
        RuntimeError
            If the CRM endpoint is unreachable or returns an unexpected status.
        """
        supported = sorted(
            set(_PERMISSION_ENDPOINT.keys()) & set(_PERMISSION_VALIDATOR.keys())
        )

        endpoint_path = _PERMISSION_ENDPOINT.get(crm_type)
        if endpoint_path is None:
            raise ValueError(
                f"crm_type '{crm_type}' has no permission-check endpoint configured. "
                f"Supported types: {supported}"
            )

        ValidatorClass = _PERMISSION_VALIDATOR.get(crm_type)
        if ValidatorClass is None:
            raise ValueError(
                f"crm_type '{crm_type}' has no permission validator registered. "
                f"Supported types: {supported}"
            )

        url = base_url + endpoint_path
        auth_headers = self._build_auth_headers(crm_type, request)

        logger.info("Checking CRM permissions for crm_type='%s' at '%s'", crm_type, url)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=auth_headers)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Could not reach the {crm_type} permission endpoint at '{url}': {exc}"
            ) from exc

        if response.status_code == 401:
            raise PermissionValidationError([
                f"Authentication failed (HTTP 401). "
                f"The supplied API token was rejected by {crm_type} at '{url}'."
            ])

        if response.status_code == 403:
            raise PermissionValidationError([
                f"Access denied (HTTP 403). "
                f"The API token does not have permission to call '{url}' on {crm_type}."
            ])

        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"Permission endpoint '{url}' returned HTTP {response.status_code}. "
                f"Body: {response.text[:300]}"
            )

        try:
            body = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"Permission endpoint '{url}' returned a non-JSON body: {exc}"
            ) from exc

        validator = ValidatorClass(body)
        result = validator.validate()

        if not result.ok:
            logger.warning(
                "Permission check failed for crm_type='%s'. Failures: %s",
                crm_type, result.failures,
            )
            raise PermissionValidationError(result.failures)

        logger.info("Permission check passed for crm_type='%s'.", crm_type)

    async def _get_key_for_tenant(self, tenant_id: UUID) -> Tuple[str, str]:
        """
        Fetch the ACTIVE encryption key for a tenant.

        Lookup order:
        1. TENANT_ACTIVE_VERSION_<tenant_id> → TENANT_KEY_<tenant_id>_<version>
           (per-tenant versioned key — all new tenants)
        2. Global ACTIVE_KEY_VERSION → ENCRYPTION_KEY_<version>
           (legacy tenants created before per-tenant key rollout)

        Returns
        -------
        (version, raw_key)
        """
        result = await self._key_manager.get_active_tenant_key_and_version(str(tenant_id))
        if result is not None:
            return result

        # Fallback to global key for legacy tenants
        version, raw_key = await self._key_manager.get_active_key_and_version()
        return version, raw_key

    async def _get_key_for_row(self, row: CrmIntegration) -> Tuple[str, str]:
        """
        Fetch the correct decryption key for an EXISTING integration row.

        Try order:
        1. TENANT_KEY_<tenant_id>_<key_version>  (per-tenant versioned key)
        2. ENCRYPTION_KEY_<key_version>           (global/legacy key)
        """
        kv = row.key_version

        raw_key = await self._key_manager.get_tenant_key(str(row.tenant_id), kv)
        if raw_key is not None:
            return kv, raw_key

        raw_key = await self._key_manager.get_encryption_key(kv)
        return kv, raw_key

    async def _get_row(self, integration_id: UUID) -> Optional[CrmIntegration]:
        result = await self._db.execute(
            select(CrmIntegration).where(CrmIntegration.id == integration_id)
        )
        return result.scalar_one_or_none()

    async def _get_row_or_raise(self, integration_id: UUID) -> CrmIntegration:
        row = await self._get_row(integration_id)
        if row is None:
            raise CredentialNotFoundError(str(integration_id))
        return row

    async def _create_row(
        self,
        integration_id: UUID,
        tenant_id: UUID,
        crm_type: str,
        auth_type: str,
        key_version: str,
        base_url: str,
        credential_enc: str,
        webhook_secrets_enc: str | None,
        source_system: SourceSystem | None = None,
    ) -> CrmIntegration:
        if source_system is None:
            from app.models.source_system import SourceSystem as _SS
            result = await self._db.execute(
                select(_SS).where(_SS.system_name == crm_type)
            )
            source_system = result.scalar_one_or_none()
            if source_system is None:
                raise ValueError(
                    f"Unknown crm_type '{crm_type}'. "
                    "Register it in the source_systems table before provisioning."
                )

        row = CrmIntegration(
            id=integration_id,
            tenant_id=tenant_id,
            source_system_id=source_system.id,
            auth_type=auth_type,
            key_version=key_version,
            base_url=base_url,
            credential_enc=credential_enc,
            webhook_secrets_enc=webhook_secrets_enc,
            is_active=True,
        )
        self._db.add(row)
        await self._db.flush()
        return row

    def _build_auth_headers(
        self,
        crm_type: str,
        request: ProvisionCredentialsRequest,
    ) -> Dict[str, str]:
        import base64

        secret_dict = request.credentials.to_secret_dict()
        auth_type = request.credentials.auth_type
        headers: Dict[str, str] = {"Content-Type": "application/json"}

        if crm_type == "espocrm":
            if auth_type in ("api_key", "api_token", "access_token"):
                token = secret_dict.get("token", secret_dict.get("api_key", ""))
                headers["X-Api-Key"] = token
            elif auth_type == "basic_auth":
                username = secret_dict.get("username", "")
                password = secret_dict.get("password", "")
                encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            else:
                token = secret_dict.get("token", "")
                headers["Authorization"] = f"Bearer {token}"
        elif crm_type == "zammad":
            token = secret_dict.get("token", secret_dict.get("api_key", ""))
            headers["Authorization"] = f"Token token={token}"
        else:
            token = secret_dict.get("token", secret_dict.get("api_key", ""))
            headers["Authorization"] = f"Bearer {token}"

        return headers

    def _to_status(self, row: CrmIntegration) -> CredentialStatusResponse:
        return CredentialStatusResponse(
            integration_id=row.id,
            crm_type=row.source_system.system_name,
            auth_type=row.auth_type,
            base_url=row.base_url or "",
            key_version=row.key_version,
            is_active=row.is_active,
            has_credentials=row.has_credentials(),
            has_webhook_secrets=row.has_webhook_secrets(),
            webhook_uuid=row.webhook_uuid,
            token_expires_at=row.token_expires_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def _secret_dict_to_envelope_creds(
    auth_type: str,
    secret_dict: Dict[str, Any],
) -> Dict[str, Any]:
    if auth_type in ("api_token", "bearer_token", "access_token", "api_key"):
        return {"strategy": "api_token", "token": secret_dict.get("token", "")}

    if auth_type == "basic_auth":
        return {
            "strategy": "basic",
            "username": secret_dict.get("username", ""),
            "password": secret_dict.get("password", ""),
        }

    if auth_type == "oauth2":
        return {
            "strategy": "oauth2",
            "access_token": secret_dict.get("access_token", ""),
            "refresh_token": secret_dict.get("refresh_token"),
            "token_type": secret_dict.get("token_type", "Bearer"),
            "expires_at": secret_dict.get("expires_at"),
        }

    if auth_type == "hmac":
        return {
            "strategy": "api_token",
            "token": secret_dict.get("api_token", ""),
        }

    logger.warning(
        "Unrecognised auth_type '%s', falling back to api_token strategy", auth_type
    )
    return {"strategy": "api_token", "token": secret_dict.get("token", "")}