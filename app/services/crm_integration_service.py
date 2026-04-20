"""
app/services/crm_integration_service.py
=========================================
Example service showing the full encrypt → store → fetch → decrypt flow.

This is a reference implementation — adapt it to your actual service layer.

SAVE FLOW (create/update integration)
--------------------------------------
1. Accept plaintext credentials from the caller (API request / internal call).
2. Fetch ACTIVE_KEY_VERSION + ENCRYPTION_KEY_<version> from Infisical.
3. Build EncryptionService.
4. Encrypt each sensitive field → store JSON string in the _enc column.
5. Store key_version in the DB row — never store the key itself.
6. Commit.

READ FLOW (use integration at request time)
--------------------------------------------
1. Load the CrmIntegration row from DB.
2. Read row.key_version.
3. Fetch ENCRYPTION_KEY_<row.key_version> from Infisical.
4. Build EncryptionService with that key + version.
5. Decrypt the _enc columns you need.
6. Use plaintext values to call the CRM API — never persist decrypted values.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.credentials.async_manager import AsyncInfisicalKeyManager
from app.credentials.encryption import EncryptedPayload, EncryptionService
from app.models.crm_integration import CrmIntegration


class CrmIntegrationService:
    """
    Service layer for creating, reading, and updating CRM integrations
    with encrypted credential storage.

    Parameters
    ----------
    db:
        Async SQLAlchemy session (injected via FastAPI Depends).
    key_manager:
        Async Infisical key manager (injected as app-state singleton).
    """

    def __init__(
        self,
        db: AsyncSession,
        key_manager: AsyncInfisicalKeyManager,
    ) -> None:
        self._db = db
        self._km = key_manager

    # ------------------------------------------------------------------
    # CREATE — encrypt and store a new integration
    # ------------------------------------------------------------------

    async def create_integration(
        self,
        *,
        tenant_id: uuid.UUID,
        source_system_id: int,
        auth_type: str,
        base_url: Optional[str],
        # Plaintext sensitive fields — accept whatever is relevant for auth_type
        api_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        webhook_secrets: Optional[dict] = None,
        token_expires_at=None,
    ) -> CrmIntegration:
        """
        Encrypt credentials and persist a new CrmIntegration row.

        Returns the persisted row (with id, key_version, etc. filled in).
        """
        # 1. Fetch active key from Infisical
        version, raw_key = await self._km.get_active_key_and_version()
        svc = EncryptionService(raw_key=raw_key, key_version=version)

        # 2. Encrypt each sensitive field that was provided
        credential_enc: Optional[str] = None
        if api_key:
            credential_enc = svc.encrypt(api_key).to_db_string()

        webhook_secret_enc: Optional[str] = None
        if webhook_secret:
            webhook_secret_enc = svc.encrypt(webhook_secret).to_db_string()

        webhook_secrets_enc: Optional[str] = None
        if webhook_secrets:
            webhook_secrets_enc = svc.encrypt_dict(webhook_secrets).to_db_string()

        # 3. Build the ORM row — key_version is always stored, never the raw key
        integration = CrmIntegration(
            tenant_id=tenant_id,
            source_system_id=source_system_id,
            auth_type=auth_type,
            base_url=base_url,
            credential_enc=credential_enc,
            webhook_secret_enc=webhook_secret_enc,
            webhook_secrets_enc=webhook_secrets_enc,
            key_version=version,
            token_expires_at=token_expires_at,
        )

        self._db.add(integration)
        await self._db.flush()   # populate .id before commit
        return integration

    # ------------------------------------------------------------------
    # READ — decrypt credentials for use at request time
    # ------------------------------------------------------------------

    async def get_decrypted_credentials(
        self,
        integration: CrmIntegration,
    ) -> dict:
        """
        Fetch the encryption key for this row's key_version and decrypt
        all populated _enc columns.

        Returns
        -------
        dict with keys present only if the corresponding _enc column is set:
            {
                "api_key":        str | None,
                "webhook_secret": str | None,
                "webhook_secrets": dict | None,
                "base_url":       str | None,   # plaintext, always included
                "auth_type":      str,           # plaintext, always included
            }

        The decrypted values are **never written back to the DB**.
        """
        # 1. Fetch key for this row's version (may differ from active version
        #    if the row was encrypted before a key rotation)
        raw_key = await self._km.get_encryption_key(integration.key_version)
        svc = EncryptionService(raw_key=raw_key, key_version=integration.key_version)

        # 2. Decrypt each populated column
        api_key: Optional[str] = None
        if integration.credential_enc:
            api_key = svc.decrypt_from_db(integration.credential_enc)

        webhook_secret: Optional[str] = None
        if integration.webhook_secret_enc:
            webhook_secret = svc.decrypt_from_db(integration.webhook_secret_enc)

        webhook_secrets: Optional[dict] = None
        if integration.webhook_secrets_enc:
            webhook_secrets = svc.decrypt_dict_from_db(integration.webhook_secrets_enc)

        return {
            "api_key": api_key,
            "webhook_secret": webhook_secret,
            "webhook_secrets": webhook_secrets,
            "base_url": integration.base_url,    # plaintext
            "auth_type": integration.auth_type,  # plaintext
        }

    # ------------------------------------------------------------------
    # UPDATE — re-encrypt with the active key (also used for key rotation)
    # ------------------------------------------------------------------

    async def update_credentials(
        self,
        integration: CrmIntegration,
        *,
        api_key: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        webhook_secrets: Optional[dict] = None,
    ) -> CrmIntegration:
        """
        Re-encrypt one or more credential fields with the *current* active key.

        Always upgrades key_version to the latest active version — so this
        doubles as a key-rotation operation for the row.

        Only the fields you pass will be updated; omitted fields keep their
        current encrypted value (and the old key_version for those fields is
        implicitly retained in the ciphertext — rotate all fields together
        during formal key rotation).
        """
        version, raw_key = await self._km.get_active_key_and_version()
        svc = EncryptionService(raw_key=raw_key, key_version=version)

        if api_key is not None:
            integration.credential_enc = svc.encrypt(api_key).to_db_string()

        if webhook_secret is not None:
            integration.webhook_secret_enc = svc.encrypt(webhook_secret).to_db_string()

        if webhook_secrets is not None:
            integration.webhook_secrets_enc = svc.encrypt_dict(webhook_secrets).to_db_string()

        # Always update key_version to reflect the latest active key
        integration.key_version = version

        await self._db.flush()
        return integration