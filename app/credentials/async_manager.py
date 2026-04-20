"""
app/credentials/async_manager.py
==================================
AsyncInfisicalKeyManager
------------------------
Non-blocking async wrapper around ``InfisicalKeyManager``.

The Infisical Python SDK is fully synchronous (uses ``requests`` internally).
Running it directly inside a FastAPI route blocks the event loop.
This wrapper offloads every SDK call to a thread-pool executor so
the interface is async-compatible without rewriting the underlying manager.

Usage
-----
    # At startup (lifespan hook)
    key_manager = await AsyncInfisicalKeyManager.create()

    # In a route / service — encrypt a new credential
    version, raw_key = await key_manager.get_active_key_and_version()
    svc     = EncryptionService(raw_key=raw_key, key_version=version)
    payload = svc.encrypt(api_token)
    row.credential_enc = payload.to_db_string()
    row.key_version    = version

    # In a route / service — decrypt an existing credential
    raw_key = await key_manager.get_encryption_key(row.key_version)
    svc     = EncryptionService(raw_key=raw_key, key_version=row.key_version)
    token   = svc.decrypt_from_db(row.credential_enc)

Shutdown
--------
    await key_manager.close()      # in FastAPI shutdown lifespan hook
    # or use as async context manager:
    async with AsyncInfisicalKeyManager.create() as key_manager:
        ...

Thread-safety
-------------
The underlying SDK client is not documented as thread-safe, so every call
acquires a single ``asyncio.Lock`` before dispatching to the thread pool.
Conservative but correct — replace with per-call clients if profiling shows
lock contention under heavy load.
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
    Async façade over ``InfisicalKeyManager``.

    Parameters
    ----------
    settings:
        Infisical configuration.  Use ``InfisicalSettings.from_env()`` for
        production; inject a test double in unit tests.
    max_workers:
        Size of the dedicated thread pool.  Default 4 is sufficient for
        low-to-medium load.
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
    # Async constructor / factory
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        settings: Optional[InfisicalSettings] = None,
        max_workers: int = 4,
    ) -> "AsyncInfisicalKeyManager":
        """
        Preferred factory method.

        Reads settings from env if not provided, constructs the sync manager
        in a thread (so SDK auth I/O doesn't block the event loop), and
        returns a ready-to-use instance.

        Raises
        ------
        InfisicalConfigError
            If environment variables are missing or SDK auth fails.
        """
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
            "AsyncInfisicalKeyManager ready (workers=%d).",
            self._max_workers,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Shut down the thread pool.  Call in your FastAPI shutdown hook."""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
            logger.info("AsyncInfisicalKeyManager thread pool shut down.")

    async def __aenter__(self) -> "AsyncInfisicalKeyManager":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Public async API — mirrors InfisicalKeyManager exactly
    # ------------------------------------------------------------------

    async def get_active_key_version(self) -> str:
        """
        Async version of ``InfisicalKeyManager.get_active_key_version``.

        Returns the active version tag (e.g. ``"v1"``).
        """
        return await self._run(
            self._sync_manager.get_active_key_version,  # type: ignore[union-attr]
        )

    async def get_encryption_key(self, version: str) -> str:
        """
        Async version of ``InfisicalKeyManager.get_encryption_key``.

        Parameters
        ----------
        version:
            Key version tag from a DB row's ``key_version`` column, e.g. ``"v1"``.

        Returns
        -------
        str
            Raw key string for use in ``EncryptionService``.
        """
        return await self._run(
            self._sync_manager.get_encryption_key,  # type: ignore[union-attr]
            version,
        )

    async def get_active_key_and_version(self) -> Tuple[str, str]:
        """
        Async version of ``InfisicalKeyManager.get_active_key_and_version``.

        Returns
        -------
        (version, raw_key)
            Ready to pass directly into ``EncryptionService``.

        Example
        -------
            version, raw_key = await key_manager.get_active_key_and_version()
            svc = EncryptionService(raw_key=raw_key, key_version=version)
            payload = svc.encrypt(api_token)
            row.credential_enc = payload.to_db_string()
            row.key_version    = version
        """
        return await self._run(
            self._sync_manager.get_active_key_and_version,  # type: ignore[union-attr]
        )

    # ------------------------------------------------------------------
    # Thread-pool dispatch helper
    # ------------------------------------------------------------------

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        """
        Execute *fn(*args)* in the thread pool under the serialisation lock.
        Propagates any exception raised by *fn* unchanged.
        """
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
                "AsyncInfisicalKeyManager is not initialised. "
                "Use `await AsyncInfisicalKeyManager.create()` first."
            )