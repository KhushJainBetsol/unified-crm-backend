"""
app/services/credential_service.py

CredentialProvisioningService
==============================
Orchestrates the full lifecycle of CRM credentials:
  PROVISION  — generate integration_id → encrypt secrets → store in DB
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

Flow diagram (PROVISION)
------------------------

  Frontend / API
       │
       ▼  ProvisionCredentialsRequest + tenant_id (from JWT)
  CredentialProvisioningService.provision()
       │
       ├─► uuid4()  ← integration_id generated HERE, never from caller
       │
       ├─► AsyncInfisicalCredentialManager.get_active_key_and_version()
       │         └─► Infisical: ACTIVE_KEY_VERSION + ENCRYPTION_KEY_V<n>
       │
       ├─► EncryptionService.encrypt(outbound_secret_json) → credential_enc
       │
       ├─► EncryptionService.encrypt(webhook_secret_json)  → webhook_secrets_enc
       │         (skipped / NULL when no webhook secrets supplied)
       │
       └─► DB write:
               crm_integrations.id                 = <new uuid>
               crm_integrations.auth_type           = "api_token" | "hmac" | …
               crm_integrations.key_version         = "v1"
               crm_integrations.base_url            = "https://…"
               crm_integrations.credential_enc      = <encrypted blob>
               crm_integrations.webhook_secrets_enc = <encrypted blob> | NULL

Architecture notes
------------------
- integration_id is ALWAYS generated server-side (uuid4). Callers never supply it.
- Secrets NEVER leave this service as plaintext (only encrypted or inside envelope).
- auth_type / key_version / base_url are plain columns — queryable without decryption.
- crm_type is accessed via row.source_system.system_name (relationship, lazy=joined).
- Both _enc columns are re-keyed together in rotate() for atomicity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

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
from app.schemas.credentials import (
    CredentialStatusResponse,
    ProvisionCredentialsRequest,
    UpdateCredentialsRequest,
)

logger = logging.getLogger(__name__)


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

    # ── PROVISION ─────────────────────────────────────────────────────────

    async def provision(
        self,
        tenant_id: UUID,
        request: ProvisionCredentialsRequest,
    ) -> CredentialStatusResponse:
        """
        Encrypt and store credentials for a NEW integration.

        The integration_id is generated here — callers never supply it.
        Both credential_enc (outbound) and webhook_secrets_enc (inbound) are
        encrypted with the same active key in a single round-trip to Infisical.

        Steps
        -----
        1. Generate a fresh integration_id (uuid4).
        2. Fetch the active AES key + version from Infisical.
        3. Encrypt outbound secret dict → credential_enc.
        4. Build webhook secret dict from request; encrypt if present, else NULL.
        5. Insert a new CrmIntegration row with direct plain columns.
        """
        integration_id = uuid4()

        # ── 1. Fetch AES key ──────────────────────────────────────────────
        version, raw_key = await self._key_manager.get_active_key_and_version()
        enc_service = EncryptionService(raw_key=raw_key, key_version=version)

        # ── 2. Encrypt outbound credentials ───────────────────────────────
        secret_dict = request.credentials.to_secret_dict()
        credential_enc = enc_service.encrypt(json.dumps(secret_dict)).to_db_string()

        # ── 3. Encrypt inbound webhook secrets (nullable) ─────────────────
        webhook_secret_dict = request.build_webhook_secret_dict()
        webhook_secrets_enc: str | None = None
        if webhook_secret_dict:
            webhook_secrets_enc = enc_service.encrypt(
                json.dumps(webhook_secret_dict)
            ).to_db_string()

        # ── 4. Resolve plain column values ────────────────────────────────
        base_url = str(request.base_url).rstrip("/")
        auth_type = request.credentials.auth_type
        crm_type = request.crm_type.strip().lower()

        # ── 5. Insert ─────────────────────────────────────────────────────
        row = await self._create_row(
            integration_id=integration_id,
            tenant_id=tenant_id,
            crm_type=crm_type,
            auth_type=auth_type,
            key_version=version,
            base_url=base_url,
            credential_enc=credential_enc,
            webhook_secrets_enc=webhook_secrets_enc,
        )

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

            # If HMAC credentials also carry webhook secrets, update that column too
            cred_ws = request.credentials.to_webhook_secret_dict() \
                if hasattr(request.credentials, "to_webhook_secret_dict") else None
            if cred_ws:
                row.webhook_secrets_enc = enc_service.encrypt(
                    json.dumps(cred_ws)
                ).to_db_string()

        # ── Inbound webhook secrets (independent update) ───────────────────
        if request.has_webhook_updates():
            # Re-use the version already on the row unless credentials were
            # also updated in this same request (in which case enc_service / version
            # are already set from the block above).
            ws_version = row.key_version
            ws_raw_key = await self._key_manager.get_encryption_key(ws_version)
            ws_enc = EncryptionService(raw_key=ws_raw_key, key_version=ws_version)

            ws_dict = request.build_webhook_secret_dict()
            if ws_dict:
                row.webhook_secrets_enc = ws_enc.encrypt(
                    json.dumps(ws_dict)
                ).to_db_string()
            else:
                # Caller sent the fields but both were empty/None — clear the column
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

        # ── Decrypt webhook secrets (nullable) ─────────────────────────────
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
                "webhook_secrets": webhook_secrets,  # empty dict when not present
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

        # Decrypt with OLD key
        old_raw_key = await self._key_manager.get_encryption_key(old_version)
        old_enc = EncryptionService(raw_key=old_raw_key, key_version=old_version)

        decrypted_cred = old_enc.decrypt_from_db(row.credential_enc)

        decrypted_webhook: str | None = None
        if row.webhook_secrets_enc:
            decrypted_webhook = old_enc.decrypt_from_db(row.webhook_secrets_enc)

        # Re-encrypt with NEW (current active) key
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
                "rotated_webhook_secrets": decrypted_webhook is not None,
            },
        )
        return {
            "integration_id": str(integration_id),
            "old_key_version": old_version,
            "new_key_version": new_version,
            "rotated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── REVOKE ─────────────────────────────────────────────────────────────

    async def revoke(self, integration_id: UUID, *, wipe: bool = False) -> None:
        """
        Soft-disable an integration (is_active=False).
        Pass wipe=True to also null out BOTH _enc columns (hard delete of secrets).
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

        await self._db.commit()

        logger.info(
            "Revoked integration",
            extra={"integration_id": str(integration_id), "wipe": wipe},
        )

    # ── Private helpers ────────────────────────────────────────────────────

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
    ) -> CrmIntegration:
        """
        Insert a new CrmIntegration row.

        Resolves source_system_id from crm_type — raises ValueError if the
        crm_type is not present in the source_systems table.

        All secret-bearing fields come pre-encrypted; this method never
        handles plaintext.
        """
        from app.models.source_system import SourceSystem  # avoid circular dep

        result = await self._db.execute(
            select(SourceSystem).where(SourceSystem.system_name == crm_type)
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
# Pure helper functions (no class coupling — easier to test in isolation)
# ---------------------------------------------------------------------------


def _to_status(row: CrmIntegration) -> CredentialStatusResponse:
    """Map a CrmIntegration ORM row → CredentialStatusResponse. No secrets exposed."""
    return CredentialStatusResponse(
        integration_id=row.id,
        crm_type=row.source_system.system_name,   # via lazy="joined" relationship
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
        # api_token drives outbound calls.
        # Webhook secrets are in the envelope's metadata["webhook_secrets"].
        return {
            "strategy": "api_token",
            "token": secret_dict.get("api_token", ""),
        }

    # Fallback — keeps adapters from crashing on unrecognised auth types
    logger.warning(
        "Unrecognised auth_type '%s', falling back to api_token strategy", auth_type
    )
    return {"strategy": "api_token", "token": secret_dict.get("token", "")}