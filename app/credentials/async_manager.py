"""
app/credentials/async_manager.py
==================================
AsyncInfisicalCredentialManager  (v3 — versioned tenant keys)
--------------------------------------------------------------
Non-blocking async wrapper around ``InfisicalCredentialManager``.

The Infisical Python SDK is fully synchronous (uses ``requests`` internally).
Running it directly inside a FastAPI route blocks the event loop.
This wrapper offloads every SDK call to a thread-pool executor.

Per-tenant key API changes (v3)
--------------------------------
- ``generate_and_store_tenant_key(tenant_id)``
    Returns ``(version, raw_key)`` — the caller stores ``version`` in
    ``crm_integrations.key_version`` (e.g. "v1") instead of the literal "tenant".

- ``get_tenant_key(tenant_id, version)``
    Requires explicit version. Fetches TENANT_KEY_<tenant_id>_<version>.

- ``get_active_tenant_key_and_version(tenant_id)``
    Reads TENANT_ACTIVE_VERSION_<tenant_id> then fetches the key for that version.

- ``delete_tenant_key(tenant_id, version)``
    Used by the rotation scheduler to purge old keys from Infisical after
    all DB rows have been re-encrypted.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Tuple, TypeVar

from app.credentials.exceptions import InfisicalConfigError
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.models import InfisicalSettings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncInfisicalCredentialManager:
    """
    Async façade over ``InfisicalCredentialManager``.

    Parameters
    ----------
    settings:
        Infisical configuration.
    max_workers:
        Thread pool size.
    """

    def __init__(
        self,
        settings: InfisicalSettings,
        max_workers: int = 4,
    ) -> None:
        self._settings = settings
        self._max_workers = max_workers
        self._sync_manager: Optional[InfisicalCredentialManager] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Factory / lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        settings: Optional[InfisicalSettings] = None,
        max_workers: int = 4,
    ) -> "AsyncInfisicalCredentialManager":
        if settings is None:
            settings = InfisicalSettings.from_env()
        instance = cls(settings=settings, max_workers=max_workers)
        await instance._initialise()
        return instance

    async def _initialise(self) -> None:
        """Construct the sync manager off the event loop."""
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="infisical-worker",
        )
        loop = asyncio.get_event_loop()
        self._sync_manager = await loop.run_in_executor(
            self._executor,
            lambda: InfisicalCredentialManager(self._settings),
        )
        logger.info(
            "AsyncInfisicalCredentialManager ready (workers=%d).",
            self._max_workers,
        )

    async def close(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
            logger.info("AsyncInfisicalCredentialManager thread pool shut down.")

    async def __aenter__(self) -> "AsyncInfisicalCredentialManager":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Global AES key methods (legacy)
    # ------------------------------------------------------------------

    async def get_active_key_version(self) -> str:
        return await self._run(self._sync_manager.get_active_key_version)  # type: ignore[union-attr]

    async def get_encryption_key(self, version: str) -> str:
        return await self._run(self._sync_manager.get_encryption_key, version)  # type: ignore[union-attr]

    async def get_active_key_and_version(self) -> Tuple[str, str]:
        return await self._run(self._sync_manager.get_active_key_and_version)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Per-tenant versioned key methods
    # ------------------------------------------------------------------

    async def generate_and_store_tenant_key(self, tenant_id: str) -> Tuple[str, str]:
        """
        Generate a new versioned AES key for the tenant.

        Returns
        -------
        (version, raw_key)
            ``version`` should be stored in ``crm_integrations.key_version``
            (e.g. "v1", "v2") so the rotation scheduler can identify which
            Infisical secret to fetch for each row.
        """
        return await self._run(
            self._sync_manager.generate_and_store_tenant_key,  # type: ignore[union-attr]
            tenant_id,
        )

    async def get_tenant_key(self, tenant_id: str, version: str) -> Optional[str]:
        """
        Fetch the raw AES key for a specific tenant + version.

        Returns None if the secret does not exist.
        """
        return await self._run(
            self._sync_manager.get_tenant_key,  # type: ignore[union-attr]
            tenant_id,
            version,
        )

    async def get_active_tenant_key_and_version(
        self, tenant_id: str
    ) -> Optional[Tuple[str, str]]:
        """
        Fetch (version, raw_key) for the tenant's currently active key.
        Returns None if no per-tenant key exists yet (legacy/old tenant).
        """
        return await self._run(
            self._sync_manager.get_active_tenant_key_and_version,  # type: ignore[union-attr]
            tenant_id,
        )

    async def get_tenant_active_version(self, tenant_id: str) -> Optional[str]:
        """Return the active version string ("v1", "v2", …) or None."""
        return await self._run(
            self._sync_manager.get_tenant_active_version,  # type: ignore[union-attr]
            tenant_id,
        )

    async def delete_tenant_key(self, tenant_id: str, version: str) -> None:
        """
        Delete TENANT_KEY_<tenant_id>_<version> from Infisical.
        Called by the rotation scheduler after re-encryption is confirmed.
        """
        return await self._run(
            self._sync_manager.delete_tenant_key,  # type: ignore[union-attr]
            tenant_id,
            version,
        )

    # ------------------------------------------------------------------
    # Thread-pool dispatch
    # ------------------------------------------------------------------

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        self._assert_ready()
        loop = asyncio.get_event_loop()
        async with self._lock:
            return await loop.run_in_executor(
                self._executor,
                functools.partial(fn, *args),
            )

    def _assert_ready(self) -> None:
        if self._sync_manager is None or self._executor is None:
            raise RuntimeError(
                "AsyncInfisicalCredentialManager is not initialised. "
                "Use `await AsyncInfisicalCredentialManager.create()` first."
            )