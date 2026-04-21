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
                                    │  3. AES decrypt     │──► plaintext token
                                    │  4. Build envelope  │──► CrmCredentialEnvelope
                                    └─────────────────────┘
                                             │
                                             ▼
                                    factory builds adapter
                                    (BaseCrmClient + adapter)

This class implements the exact same interface as the mock vault used in
the test scripts — it exposes get_credentials(integration_id) — so the
factory requires zero changes.

Why not store creds in Infisical?
---------------------------------
The team's architecture intentionally keeps credentials in PostgreSQL
(encrypted with AES-256-CBC).  Infisical holds only the encryption keys.
This gives:
  - Full audit trail of integration records in the DB
  - Tenant-scoped credential lifecycle (FK → tenants)
  - Key rotation without credential re-entry (just rotate the AES key)
  - Infisical used for what it's best at: key management, not data storage
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
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
        For async usage, use AsyncDbBackedCredentialService instead.

    Usage (sync — for scheduler, scripts, CLI)
    -------------------------------------------
    service = DbBackedCredentialService(
        key_manager=InfisicalCredentialManager(settings),
        db_session_factory=sync_session_maker,
    )
    envelope = service.get_credentials("550e8400-e29b-41d4-...")
    adapter  = factory.create("550e8400-e29b-41d4-...")
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
        1. Look up CrmIntegration row by integration_id (= row.id).
        2. Fetch the AES key for row.key_version from Infisical.
        3. Decrypt row.credential_enc using EncryptionService.
        4. Build and return a CrmCredentialEnvelope in memory.

        Nothing is stored — the envelope lives only for the duration of
        the request / sync operation.

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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_envelope(
        self, row: Any, integration_id: str
    ) -> CrmCredentialEnvelope:
        """Decrypt the DB row and build a CrmCredentialEnvelope."""
        crm_type = (
            row.source_system.system_name
            if row.source_system
            else "unknown"
        )

        # Step 1 — fetch AES key for this row's key_version from Infisical
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

        # Step 2 — decrypt credential_enc
        if not row.credential_enc:
            raise CredentialDecodeError(
                integration_id,
                "credential_enc column is empty — integration was never provisioned.",
            )

        try:
            decrypted_token = enc_service.decrypt_from_db(row.credential_enc)
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"AES decryption failed: {exc}",
            ) from exc

        # Step 3 — build credentials dict from auth_type + decrypted value
        credentials_dict = _build_credentials_dict(row.auth_type, decrypted_token)

        # Step 4 — build and validate envelope
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


# ---------------------------------------------------------------------------
# Async version — used by FastAPI routes and the async factory
# ---------------------------------------------------------------------------

class AsyncDbBackedCredentialService:
    """
    Async version of DbBackedCredentialService for FastAPI routes.

    The Infisical SDK is sync, so key fetching is offloaded to the
    thread pool (same pattern as AsyncInfisicalCredentialManager).

    Parameters
    ----------
    key_manager:
        Sync InfisicalCredentialManager (key fetching).
    async_session_factory:
        Callable that returns an AsyncSession (your async_session_maker).

    Usage (in FastAPI — stored on app.state)
    -----------------------------------------
    service = AsyncDbBackedCredentialService(
        key_manager=InfisicalCredentialManager(settings),
        async_session_factory=async_session_maker,
    )
    # Store on app.state at startup:
    app.state.credential_service = service

    # In factory / route:
    envelope = await service.get_credentials(integration_id)
    """

    def __init__(
        self,
        key_manager: InfisicalCredentialManager,
        async_session_factory: Any,
        executor: Any = None,
    ) -> None:
        self._key_manager = key_manager
        self._async_session_factory = async_session_factory
        self._executor = executor  # ThreadPoolExecutor from app.state

    async def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        """
        Async READ FLOW — same logic as DbBackedCredentialService.get_credentials
        but with async DB access and thread-pool Infisical calls.
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

        # ── 3. Decrypt in thread pool (CPU work, non-blocking) ────────────
        enc_service = EncryptionService(
            raw_key=raw_key, key_version=row.key_version
        )
        try:
            decrypted_token = await loop.run_in_executor(
                self._executor,
                functools.partial(enc_service.decrypt_from_db, row.credential_enc),
            )
        except Exception as exc:
            raise CredentialDecodeError(
                integration_id,
                f"AES decryption failed: {exc}",
            ) from exc

        # ── 4. Build envelope ─────────────────────────────────────────────
        credentials_dict = _build_credentials_dict(row.auth_type, decrypted_token)
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
# Shared helper — maps auth_type → credentials dict shape
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

    The decrypted_value is the raw string from credential_enc
    (API key, bearer token, or "username:password" for basic auth).
    """
    strategy = _AUTH_TYPE_TO_STRATEGY.get(auth_type, "api_token")

    if strategy == "api_token":
        return {"strategy": "api_token", "token": decrypted_value}

    if strategy == "basic":
        # Stored format: "username:password"
        if ":" in decrypted_value:
            username, _, password = decrypted_value.partition(":")
        else:
            username = decrypted_value
            password = ""
        return {"strategy": "basic", "username": username, "password": password}

    # oauth2 — future: credential_enc would store JSON with refresh_token etc.
    return {"strategy": "api_token", "token": decrypted_value}