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

Two-column secret model
-----------------------
  credential_enc      → outbound auth secrets (api token, password, OAuth tokens…)
  webhook_secrets_enc → inbound webhook HMAC secrets (nullable — always may be None)

Both columns are encrypted independently with the same key version so a
single rotate() call re-encrypts both atomically.

Permission gate (PROVISION + CHECK-CONNECTION)
----------------------------------------------
Before any encryption or DB write, provision() calls _check_permissions()
which hits the CRM's permission-inspection endpoint and validates the
response using the appropriate CrmPermissionValidator subclass.

  Unknown crm_type          →  ValueError raised immediately with a list of
                               supported types. Nothing is written to DB.

  Permissions INSUFFICIENT  →  PermissionValidationError is raised immediately
                               with a list of exactly which checks failed.
                               Nothing is written to DB.

  Permissions SUFFICIENT    →  normal encryption + DB write proceeds.

Upsert logic (PROVISION)
--------------------------
provision() now checks TenantSourceSystem before writing anything:

  If a TenantSourceSystem row already exists for (tenant_id, source_system_id):
      → permission check runs first (same as fresh provision)
      → delegates to update() using the existing integration_id
      → sets is_active=True on the row (re-activates if previously revoked)

  If no row exists:
      → normal create path (encrypt → insert CrmIntegration → insert TenantSourceSystem)

This means the frontend can always call POST /integrations/ and the backend
decides whether to create or update. The frontend never needs to know.

Flow diagram (PROVISION — upsert)
----------------------------------

  Frontend / API
       │
       ▼  POST /integrations/  (always)
  CredentialProvisioningService.provision()
       │
       ├─► Fetch tenant name
       │
       ├─► Resolve SourceSystem from crm_type
       │       └─► ValueError if unknown crm_type  ◄─ STOPS HERE
       │
       ├─► Lookup TenantSourceSystem (tenant_id + source_system_id)
       │       │
       │  ┌────┴────┐
       │  │         │
       │ EXISTS   NOT FOUND
       │  │         │
       │  ▼         ▼
       │ _check_permissions()   _check_permissions()
       │  │                      │
       │  ▼                      ▼
       │ update(existing_id)    encrypt → insert CrmIntegration
       │  └─► is_active=True    insert TenantSourceSystem
       │
       └─► return CredentialStatusResponse

Architecture notes
------------------
- integration_id is ALWAYS generated server-side (uuid4). Callers never supply it.
- Secrets NEVER leave this service as plaintext (only encrypted or inside envelope).
- auth_type / key_version / base_url are plain columns — queryable without decryption.
- crm_type is accessed via row.source_system.system_name (relationship, lazy=joined).
- Both _enc columns are re-keyed together in rotate() for atomicity.
- Permission check uses the raw credentials from the request (before encryption)
  so no Infisical round-trip is needed at validation time.
- update() always sets is_active=True when credentials are supplied so that
  a previously revoked/wiped integration is re-activated on re-provision.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.encryption import EncryptionService
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialNotFoundError,
)
from app.credentials.models import CrmCredentialEnvelope
from app.models.crm_integration import CrmIntegration
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

# ---------------------------------------------------------------------------
# CRM permission-check endpoint registry
#
# Maps crm_type (lowercase, as stored in source_systems.system_name) to the
# path that returns the permission/ACL payload for the authenticated user.
# The full URL is assembled as:  base_url + path
#
# IMPORTANT: Any crm_type NOT present here will be REJECTED by
# _check_permissions() with a ValueError. Add new CRM types here (and a
# matching entry in _PERMISSION_VALIDATOR) before provisioning them.
# ---------------------------------------------------------------------------
_PERMISSION_ENDPOINT: Dict[str, str] = {
    "espocrm": "/api/v1/App/user",
    "zammad":  "/api/v1/user_access_token",
}

# Maps crm_type → validator class
_PERMISSION_VALIDATOR = {
    "espocrm": EspoCrmPermissionValidator,
    "zammad":  ZammadPermissionValidator,
}


class CredentialProvisioningService:
    """
    Async service for the full credential lifecycle.

    Parameters
    ----------
    key_manager:
        Async Infisical manager that holds and retrieves AES keys.
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

        If the tenant already has an integration row for this CRM type
        (determined by looking up TenantSourceSystem), the existing row is
        updated (re-encrypted + is_active restored).  Otherwise a fresh
        integration_id is generated and a new row is created.

        In both cases the permission gate runs BEFORE any DB write.

        Steps (create path)
        -------------------
        1. Fetch tenant name from DB.
        2. Resolve SourceSystem — raises ValueError for unknown crm_type.
        3. Check TenantSourceSystem for existing integration.
           → If found: run permission check → delegate to update() → return.
        4. Permission gate — raises on failure, nothing written.
        5. Generate fresh integration_id (uuid4).
        6. Fetch active AES key + version from Infisical.
        7. Encrypt outbound secret dict → credential_enc.
        8. Encrypt webhook secret dict → webhook_secrets_enc (or NULL).
        9. Insert CrmIntegration row.
        10. Fetch CRM org ID + insert TenantSourceSystem row.
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
        tenant_name = tenant.name

        # ── 2. Resolve SourceSystem (validates crm_type early) ─────────────
        ss_result = await self._db.execute(
            select(SourceSystem).where(SourceSystem.system_name == crm_type)
        )
        source_system = ss_result.scalars().first()
        if source_system is None:
            raise ValueError(
                f"Unknown crm_type '{crm_type}'. "
                "Register it in the source_systems table before provisioning."
            )

        # ── 3. Upsert check — does this tenant already have this CRM? ──────
        tss_result = await self._db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.tenant_id == tenant_id,
                TenantSourceSystem.source_system_id == source_system.id,
            )
        )
        existing_tss = tss_result.scalars().first()

        if existing_tss is not None:
            # ── Existing integration → run permission check then update ────
            logger.info(
                "Integration already exists for tenant_id='%s' crm_type='%s' "
                "(integration_id='%s'). Running upsert → update path.",
                tenant_id,
                crm_type,
                existing_tss.integration_id,
            )

            # Permission gate still runs — we don't skip it on updates.
            await self._check_permissions(
                crm_type=crm_type,
                base_url=base_url,
                request=request,
            )

            # Build an UpdateCredentialsRequest from the provision payload.
            # base_url and webhook fields are forwarded so nothing is lost.
            update_request = UpdateCredentialsRequest(
                credentials=request.credentials,
                base_url=request.base_url,
            )

            # Carry webhook fields through if the schema has them.
            # (UpdateCredentialsRequest may expose webhook_* fields depending
            #  on your schema version — assign them if present.)
            for attr in ("webhook_secret", "webhook_signing_secret"):
                if hasattr(request, attr) and hasattr(update_request, attr):
                    setattr(update_request, attr, getattr(request, attr))

            return await self.update(
                integration_id=existing_tss.integration_id,
                request=update_request,
            )

        # ── 4. Permission gate — nothing is written if this raises ─────────
        await self._check_permissions(
            crm_type=crm_type,
            base_url=base_url,
            request=request,
        )

        integration_id = uuid4()

        # ── 5. Fetch AES key ───────────────────────────────────────────────
        version, raw_key = await self._key_manager.get_active_key_and_version()
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

        # ── 8. Resolve plain column values ─────────────────────────────────
        auth_type = request.credentials.auth_type

        # ── 9. Insert CrmIntegration row ───────────────────────────────────
        row = await self._create_row(
            integration_id=integration_id,
            tenant_id=tenant_id,
            crm_type=crm_type,
            auth_type=auth_type,
            key_version=version,
            base_url=base_url,
            credential_enc=credential_enc,
            webhook_secrets_enc=webhook_secrets_enc,
            source_system=source_system,          # already resolved — no second query
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
        return _to_status(row)

    # ── UPDATE (partial) ───────────────────────────────────────────────────

    async def update(
        self,
        integration_id: UUID,
        request: UpdateCredentialsRequest,
    ) -> CredentialStatusResponse:
        """
        Partially update credentials / metadata / webhook secrets on an
        existing integration. Only supplied fields are changed.

        Updating credentials re-encrypts with the current active key so the
        row's key_version is also bumped. Webhook secrets can be updated
        independently of outbound credentials and vice-versa.

        When credentials are supplied, is_active is set to True so that a
        previously revoked / wiped integration is cleanly re-activated on
        re-provision (the upsert path in provision() delegates here).
        """
        row = await self._get_row_or_raise(integration_id)

        # ── Outbound credentials ──────────────────────────────────────────
        if request.credentials is not None:
            version, raw_key = await self._key_manager.get_active_key_and_version()
            enc_service = EncryptionService(raw_key=raw_key, key_version=version)

            secret_dict = request.credentials.to_secret_dict()
            row.credential_enc = enc_service.encrypt(json.dumps(secret_dict)).to_db_string()
            row.auth_type = request.credentials.auth_type
            row.key_version = version

            # Re-activate in case this row was previously revoked or wiped.
            # This is the key fix for the reset → re-provision flow:
            # revoke(wipe=True) sets is_active=False; update() restores it.
            row.is_active = True

            # If HMAC credentials also carry webhook secrets, update that column too.
            cred_ws = (
                request.credentials.to_webhook_secret_dict()
                if hasattr(request.credentials, "to_webhook_secret_dict")
                else None
            )
            if cred_ws:
                row.webhook_secrets_enc = enc_service.encrypt(
                    json.dumps(cred_ws)
                ).to_db_string()

        # ── Inbound webhook secrets (independent update) ───────────────────
        if request.has_webhook_updates():
            ws_version = row.key_version
            ws_raw_key = await self._key_manager.get_encryption_key(ws_version)
            ws_enc = EncryptionService(raw_key=ws_raw_key, key_version=ws_version)

            ws_dict = request.build_webhook_secret_dict()
            if ws_dict:
                row.webhook_secrets_enc = ws_enc.encrypt(
                    json.dumps(ws_dict)
                ).to_db_string()
            else:
                row.webhook_secrets_enc = None

        # ── Plain columns ─────────────────────────────────────────────────
        if request.base_url is not None:
            row.base_url = str(request.base_url).rstrip("/")

        await self._db.commit()
        await self._db.refresh(row)

        logger.info(
            "Partially updated credentials",
            extra={"integration_id": str(integration_id)},
        )
        return _to_status(row)

    # ── RETRIEVE (decrypt → envelope) ─────────────────────────────────────

    async def get_envelope(self, integration_id: UUID) -> CrmCredentialEnvelope:
        """
        Decrypt outbound credentials and return a CrmCredentialEnvelope.
        Used internally by the adapter factory to construct CRM clients.
        Secrets never appear in logs or return values outside the envelope.

        Webhook secrets (if present) are included in the envelope's metadata
        so adapters can access them for inbound verification.
        """
        row = await self._get_row_or_raise(integration_id)

        if not row.credential_enc:
            raise CredentialNotFoundError(str(integration_id))

        key_version = row.key_version
        raw_key = await self._key_manager.get_encryption_key(key_version)
        enc_service = EncryptionService(raw_key=raw_key, key_version=key_version)

        # ── Decrypt outbound credentials ──────────────────────────────────
        try:
            decrypted_json = enc_service.decrypt_from_db(row.credential_enc)
            secret_dict: Dict[str, Any] = json.loads(decrypted_json)
        except Exception as exc:
            raise CredentialDecodeError(str(integration_id), str(exc)) from exc

        # ── Decrypt webhook secrets (nullable) ────────────────────────────
        webhook_secrets: Dict[str, Any] = {}
        if row.webhook_secrets_enc:
            try:
                webhook_secrets = json.loads(
                    enc_service.decrypt_from_db(row.webhook_secrets_enc)
                )
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt webhook_secrets_enc for integration %s: %s",
                    integration_id,
                    exc,
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
        """Return metadata/status without decrypting anything."""
        row = await self._get_row_or_raise(integration_id)
        return _to_status(row)

    # ── ROTATE ────────────────────────────────────────────────────────────

    async def rotate(self, integration_id: UUID) -> dict:
        """
        Re-encrypt BOTH _enc columns with the current active Infisical key.

        Call this on all active integrations after rotating the Infisical key
        so that all rows migrate to the new key version atomically.
        """
        row = await self._get_row_or_raise(integration_id)

        if not row.credential_enc:
            raise CredentialNotFoundError(str(integration_id))

        old_version = row.key_version

        old_raw_key = await self._key_manager.get_encryption_key(old_version)
        old_enc = EncryptionService(raw_key=old_raw_key, key_version=old_version)

        decrypted_cred = old_enc.decrypt_from_db(row.credential_enc)

        decrypted_webhook: str | None = None
        if row.webhook_secrets_enc:
            decrypted_webhook = old_enc.decrypt_from_db(row.webhook_secrets_enc)

        new_version, new_raw_key = await self._key_manager.get_active_key_and_version()
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
        Soft-disable an integration (is_active=False).
        Pass wipe=True to also null out BOTH _enc columns (hard delete of secrets).

        Also hard-deletes the associated TenantSourceSystem row so that a
        subsequent POST /integrations/ for the same tenant + crm_type will
        go through the create path (fresh uuid4, fresh TenantSourceSystem row)
        rather than the update path.

        If you want to allow re-provisioning via the upsert path (update the
        existing CrmIntegration row), keep the TenantSourceSystem row alive
        and just wipe the credential columns — pass wipe=True and handle the
        TenantSourceSystem deletion separately, or don't delete it here.

        Current behaviour: TenantSourceSystem is always deleted on revoke so
        the integration_id can be cleanly re-issued on next provision.
        """
        row = await self._get_row_or_raise(integration_id)
        row.is_active = False

        if wipe:
            row.credential_enc = None
            row.webhook_secrets_enc = None
            logger.warning(
                "Wiped credential_enc and webhook_secrets_enc",
                extra={"integration_id": str(integration_id)},
            )

        # ── Hard-delete TenantSourceSystem row ────────────────────────────
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
                "No TenantSourceSystem row found for integration_id='%s' — "
                "nothing to delete.",
                str(integration_id),
            )

        await self._db.commit()

        logger.info(
            "Revoked integration",
            extra={"integration_id": str(integration_id), "wipe": wipe},
        )

    # ── Private helpers ────────────────────────────────────────────────────

    async def _check_permissions(
        self,
        crm_type: str,
        base_url: str,
        request: ProvisionCredentialsRequest,
    ) -> None:
        """
        Hit the CRM's permission-inspection endpoint using the caller's raw
        credentials and validate the response.

        This runs BEFORE any encryption or DB write.  If the token lacks the
        required permissions a PermissionValidationError is raised — the caller
        sees a clear list of exactly which checks failed and nothing is persisted.

        An UNKNOWN crm_type raises ValueError instead of silently passing.
        This prevents placeholder or mistyped crm_type values from slipping
        through as false positives.

        Parameters
        ----------
        crm_type:
            Lowercase CRM identifier (e.g. "espocrm", "zammad").
        base_url:
            CRM base URL already stripped of trailing slash.
        request:
            The original provision request carrying the raw credentials.

        Raises
        ------
        ValueError
            crm_type is not in the known registry.
        PermissionValidationError
            One or more required permissions are missing.
        RuntimeError
            The HTTP call to the CRM's permission endpoint failed (network
            error, non-2xx response, or unparseable body).
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
        auth_headers = _build_auth_headers(crm_type, request)

        logger.info(
            "Checking CRM permissions for crm_type='%s' at '%s'",
            crm_type,
            url,
        )

        # ── HTTP call ─────────────────────────────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=auth_headers)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Could not reach the {crm_type} permission endpoint at '{url}': {exc}"
            ) from exc

        if response.status_code == 401:
            raise PermissionValidationError(
                [
                    f"Authentication failed (HTTP 401). "
                    f"The supplied API token was rejected by {crm_type} at '{url}'. "
                    "Please verify your token or API key."
                ]
            )

        if response.status_code == 403:
            raise PermissionValidationError(
                [
                    f"Access denied (HTTP 403). "
                    f"The API token does not have permission to call '{url}' on {crm_type}."
                ]
            )

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

        # ── Validate parsed response ───────────────────────────────────────
        validator = ValidatorClass(body)
        result = validator.validate()

        if not result.ok:
            logger.warning(
                "Permission check failed for crm_type='%s'. Failures: %s",
                crm_type,
                result.failures,
            )
            raise PermissionValidationError(result.failures)

        logger.info(
            "Permission check passed for crm_type='%s'.", crm_type
        )

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
        """
        Insert a new CrmIntegration row.

        Accepts an already-resolved ``source_system`` object to avoid a
        redundant DB query when provision() has already fetched it.  If not
        supplied, it is fetched here (backward-compatible).

        All secret-bearing fields come pre-encrypted; this method never
        handles plaintext.
        """
        if source_system is None:
            # Fallback — resolves source_system when called outside provision()
            from app.models.source_system import SourceSystem as _SS  # avoid circular dep
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


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _build_auth_headers(
    crm_type: str,
    request: ProvisionCredentialsRequest,
) -> Dict[str, str]:
    """
    Build the HTTP headers needed to authenticate the permission-check request
    using the RAW (un-encrypted) credentials from the provision request.

    Each CRM uses a different convention:
      - EspoCRM  → "X-Api-Key: <token>"  (api_key auth)
                   "Authorization: Basic <b64(user:pass)>"  (basic_auth)
                   "Authorization: Bearer <token>"  (api_token / bearer)
      - Zammad   → "Authorization: Token token=<token>"

    Falls back to a Bearer header for unrecognised CRM types so the
    permission endpoint can still be called rather than silently skipped.
    """
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
        # Generic fallback
        token = secret_dict.get("token", secret_dict.get("api_key", ""))
        headers["Authorization"] = f"Bearer {token}"

    return headers


def _to_status(row: CrmIntegration) -> CredentialStatusResponse:
    """Map a CrmIntegration ORM row → CredentialStatusResponse. No secrets exposed."""
    return CredentialStatusResponse(
        integration_id=row.id,
        crm_type=row.source_system.system_name,
        auth_type=row.auth_type,
        base_url=row.base_url or "",
        key_version=row.key_version,
        is_active=row.is_active,
        has_credentials=row.has_credentials(),
        has_webhook_secrets=row.has_webhook_secrets(),
        token_expires_at=row.token_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _secret_dict_to_envelope_creds(
    auth_type: str,
    secret_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert the decrypted outbound secret dict into the standard
    CrmCredentialEnvelope credentials format. Always includes a 'strategy'
    key so adapters can switch on a single field.
    """
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