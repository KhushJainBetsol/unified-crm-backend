# app/credentials/db_credential_service.py
"""
DbBackedCredentialService
==========================
The bridge between the PostgreSQL-encrypted credential store and the
CRM adapter factory.

Architecture recap
------------------
                                    ┌─────────────────────┐
  factory.create(integration_id) ──►│DbBackedCredential   │
                                    │Service              │
                                    │  1. DB lookup       │──► CrmIntegration row
                                    │  2. Infisical fetch │──► ENCRYPTION_KEY_<v>
                                    │  3. AES decrypt     │──► plaintext JSON
                                    │  4. Parse JSON      │──► token / creds dict
                                    │  5. Build envelope  │──► CrmCredentialEnvelope
                                    └─────────────────────┘
                                             │
                                             ▼
                                    factory builds adapter
                                    (BaseCrmClient + adapter)

Why not store creds in Infisical?
---------------------------------
Infisical holds only the AES encryption keys.
PostgreSQL holds the AES-encrypted credential blobs.
This gives full audit trail, tenant-scoped lifecycle, and key rotation
without credential re-entry.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select

from app.credentials.encryption import EncryptionService
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialNotFoundError,
)
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.models import CrmCredentialEnvelope

logger = logging.getLogger(__name__)


class DbBackedCredentialService:
    """
    Synchronous credential service that reads from PostgreSQL and decrypts
    using the AES key fetched from Infisical.

    Implements the same get_credentials() interface as the test MockVault,
    so the CrmAdapterFactory works without any changes.

    Parameters
    ----------
    key_manager:
        An initialised InfisicalCredentialManager (key fetching only).
    db_session_factory:
        A callable that returns a sync SQLAlchemy session.
    """

    def __init__(
        self,
        key_manager: InfisicalCredentialManager,
        db_session_factory: Any,
    ) -> None:
        self._key_manager = key_manager
        self._db_session_factory = db_session_factory

    def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        """
        READ FLOW
        ---------
        1. Look up CrmIntegration row by integration_id.
        2. Fetch the AES key for row.key_version from Infisical.
        3. Decrypt row.credential_enc → JSON string.
        4. Parse JSON → secret dict.
        5. Build and return a CrmCredentialEnvelope.

        Raises
        ------
        CredentialNotFoundError
            If no active CrmIntegration row matches integration_id.
        CredentialDecodeError
            If decryption or envelope construction fails.
        """
        from app.models.crm_integration import CrmIntegration

        with self._db_session_factory() as db:
            row: Optional[CrmIntegration] = db.get(CrmIntegration, integration_id)

        if row is None or not row.is_active:
            raise CredentialNotFoundError(integration_id)

        return self._build_envelope(row, integration_id)

    def _build_envelope(
        self, row: Any, integration_id: str
    ) -> CrmCredentialEnvelope:
        """Decrypt the DB row and build a CrmCredentialEnvelope."""
        crm_type = (
            row.source_system.system_name
            if row.source_system
            else "unknown"
        )

        try:
            raw_key = self._key_manager.get_encryption_key(row.key_version)
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"Failed to fetch encryption key version='{row.key_version}': {exc}",
            ) from exc

        enc_service = EncryptionService(
            raw_key=raw_key, key_version=row.key_version
        )

        if not row.credential_enc:
            raise CredentialDecodeError(
                integration_id,
                "credential_enc column is empty — integration was never provisioned.",
            )

        try:
            decrypted_json = enc_service.decrypt_from_db(row.credential_enc)
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"AES decryption failed: {exc}",
            ) from exc

        credentials_dict = _build_credentials_dict(row.auth_type, decrypted_json)

        try:
            envelope = CrmCredentialEnvelope(
                crm_type=crm_type,
                base_url=row.base_url or "",
                credentials=credentials_dict,
                metadata={
                    "key_version": row.key_version,
                    "auth_type": row.auth_type,
                },
            )
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"CrmCredentialEnvelope construction failed: {exc}",
            ) from exc

        logger.debug(
            "Built CrmCredentialEnvelope for integration_id='%s' "
            "(crm_type=%s, key_version=%s).",
            integration_id,
            crm_type,
            row.key_version,
        )
        return envelope


class AsyncDbBackedCredentialService:
    """
    Async version of DbBackedCredentialService for FastAPI routes and the
    async adapter factory.

    The Infisical SDK is sync, so key fetching is offloaded to a thread pool.

    Parameters
    ----------
    key_manager:
        Sync InfisicalCredentialManager (key fetching only).
    async_session_factory:
        Callable that returns an AsyncSession (async_session_maker).
    executor:
        ThreadPoolExecutor — reuse the one from AsyncInfisicalCredentialManager.
    """

    def __init__(
        self,
        key_manager: InfisicalCredentialManager,
        async_session_factory: Any,
        executor: Any = None,
    ) -> None:
        self._key_manager = key_manager
        self._async_session_factory = async_session_factory
        self._executor = executor

    async def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        """
        Async READ FLOW
        ---------------
        1. Async DB lookup by integration_id.
        2. Fetch AES key from Infisical via thread pool (sync SDK).
        3. Decrypt credential_enc in thread pool (CPU work).
        4. Parse decrypted JSON → secret dict.
        5. Build and return CrmCredentialEnvelope.

        Raises
        ------
        CredentialNotFoundError
            If no active row matches integration_id.
        CredentialDecodeError
            If decryption, JSON parse, or envelope construction fails.
        """
        import asyncio
        import functools
        from app.models.crm_integration import CrmIntegration

        # ── 1. Async DB lookup ────────────────────────────────────────────
        async with self._async_session_factory() as db:
            result = await db.execute(
                select(CrmIntegration).where(
                    CrmIntegration.id == integration_id,
                    CrmIntegration.is_active == True,
                ).limit(1)
            )
            row = result.scalar_one_or_none()

        if row is None:
            raise CredentialNotFoundError(integration_id)

        crm_type = (
            row.source_system.system_name
            if row.source_system
            else "unknown"
        )

        if not row.credential_enc:
            raise CredentialDecodeError(
                integration_id,
                "credential_enc column is empty.",
            )

        # ── 2. Fetch AES key from Infisical (sync SDK → thread pool) ─────
        loop = asyncio.get_event_loop()
        try:
            raw_key = await loop.run_in_executor(
                self._executor,
                functools.partial(
                    self._key_manager.get_encryption_key, row.key_version
                ),
            )
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"Infisical key fetch failed for version='{row.key_version}': {exc}",
            ) from exc

        # ── 3. Decrypt in thread pool ─────────────────────────────────────
        enc_service = EncryptionService(
            raw_key=raw_key, key_version=row.key_version
        )
        try:
            decrypted_json = await loop.run_in_executor(
                self._executor,
                functools.partial(enc_service.decrypt_from_db, row.credential_enc),
            )
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"AES decryption failed: {exc}",
            ) from exc

        # ── 4. Parse JSON + build envelope ────────────────────────────────
        credentials_dict = _build_credentials_dict(row.auth_type, decrypted_json)
        try:
            envelope = CrmCredentialEnvelope(
                crm_type=crm_type,
                base_url=row.base_url or "",
                credentials=credentials_dict,
                metadata={
                    "key_version": row.key_version,
                    "auth_type": row.auth_type,
                },
            )
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"Envelope construction failed: {exc}",
            ) from exc

        logger.debug(
            "AsyncDbBackedCredentialService: built envelope for "
            "integration_id='%s' (crm_type=%s).",
            integration_id,
            crm_type,
        )
        return envelope


# ---------------------------------------------------------------------------
# Shared helper — maps auth_type + decrypted JSON → credentials dict
# ---------------------------------------------------------------------------

_AUTH_TYPE_TO_STRATEGY = {
    "api_key":      "api_token",
    "api_token":    "api_token",
    "bearer_token": "api_token",
    "access_token": "api_token",
    "basic_auth":   "basic",
    "oauth2":       "oauth2",
    "hmac":         "api_token",
}


def _build_credentials_dict(auth_type: str, decrypted_value: str) -> dict:
    """
    Build the credentials dict for a CrmCredentialEnvelope.

    ``decrypted_value`` is the AES-decrypted string from credential_enc.
    CredentialProvisioningService always stores this as JSON
    (json.dumps(secret_dict)), so we parse it first.

    Falls back to treating the raw string as a token if JSON parsing fails
    (handles any legacy rows that stored a plain token string).

    Parameters
    ----------
    auth_type:
        The raw auth_type column value from CrmIntegration
        (e.g. "api_token", "access_token", "basic_auth").
    decrypted_value:
        The AES-decrypted string from credential_enc — expected to be a
        JSON object like ``{"token": "abc123", "auth_type": "access_token"}``.

    Returns
    -------
    dict
        Credentials dict with a ``"strategy"`` key, ready for
        CrmCredentialEnvelope and BaseCrmClient.build_headers().
    """
    # ── Parse the stored JSON ─────────────────────────────────────────────
    # CredentialProvisioningService.provision() stores json.dumps(secret_dict).
    # The old DbBackedCredentialService passed the raw string directly, which
    # caused the auth header to become e.g. 'Token token={"token":"abc123"}'.
    try:
        secret_dict: dict = json.loads(decrypted_value)
        if not isinstance(secret_dict, dict):
            raise ValueError("Parsed JSON is not a dict")
    except (json.JSONDecodeError, ValueError):
        # Legacy fallback: raw token string stored without JSON wrapping
        logger.warning(
            "credential_enc did not contain valid JSON for auth_type='%s'; "
            "treating raw decrypted value as token string. "
            "Re-provision this integration to store credentials in the correct format.",
            auth_type,
        )
        secret_dict = {"token": decrypted_value}

    strategy = _AUTH_TYPE_TO_STRATEGY.get(auth_type, "api_token")

    if strategy == "api_token":
        # Covers api_token, bearer_token, access_token, api_key, hmac
        token = secret_dict.get("token") or secret_dict.get("api_key", "")
        return {"strategy": "api_token", "token": token}

    if strategy == "basic":
        return {
            "strategy": "basic",
            "username": secret_dict.get("username", ""),
            "password": secret_dict.get("password", ""),
        }

    if strategy == "oauth2":
        return {
            "strategy": "oauth2",
            "access_token": secret_dict.get("access_token", ""),
            "refresh_token": secret_dict.get("refresh_token"),
            "token_type": secret_dict.get("token_type", "Bearer"),
            "expires_at": secret_dict.get("expires_at"),
        }

    # Unknown strategy — best-effort fallback
    logger.warning(
        "Unrecognised auth_type '%s', falling back to api_token strategy.", auth_type
    )
    return {
        "strategy": "api_token",
        "token": secret_dict.get("token") or secret_dict.get("api_key", ""),
    }