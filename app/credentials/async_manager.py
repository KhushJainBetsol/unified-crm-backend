# crm/credentials/async_manager.py
"""
AsyncInfisicalCredentialManager
================================
A non-blocking async wrapper around ``InfisicalCredentialManager``.

The Infisical Python SDK is fully synchronous (it uses ``requests`` under the
hood).  Running it directly inside an async FastAPI route would block the
event loop, degrading throughput under concurrent load.

This wrapper offloads every SDK call to a thread-pool executor via
``asyncio.get_event_loop().run_in_executor()``, making the interface
fully async-compatible without requiring a rewrite of the underlying manager.

Usage
-----
# At startup (lifespan hook)
manager = await AsyncInfisicalCredentialManager.create()

# In a route / service
envelope = await manager.get_credentials(integration_id)
await manager.save_credentials(integration_id, envelope)
await manager.delete_credentials(integration_id)

Thread-safety
-------------
The underlying synchronous ``InfisicalCredentialManager`` is called from
worker threads.  The Infisical SDK client itself is not documented as
thread-safe, so each high-level call acquires an ``asyncio.Lock`` to
serialise SDK access.  This is conservative but correct; replace with a
per-call client if throughput profiling shows lock contention.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from app.credentials.exceptions import InfisicalConfigError
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.models import CrmCredentialEnvelope, InfisicalSettings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncInfisicalCredentialManager:
    """
    Async façade over ``InfisicalCredentialManager``.

    Parameters
    ----------
    settings:
        Infisical configuration.  Use ``InfisicalSettings.from_env()`` for
        production; pass a test double in unit tests.
    max_workers:
        Size of the dedicated thread pool.  Defaults to 4, which is
        sufficient for low-to-medium throughput.  Increase if profiling
        shows thread starvation.
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
    ) -> "AsyncInfisicalCredentialManager":
        """
        Preferred factory method.  Reads settings from env if not provided,
        constructs the sync manager in a thread (so SDK auth I/O doesn't
        block the event loop), and returns a ready instance.

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
            "AsyncInfisicalCredentialManager ready (workers=%d).",
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
            logger.info("AsyncInfisicalCredentialManager thread pool shut down.")

    async def __aenter__(self) -> "AsyncInfisicalCredentialManager":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Public async API — mirrors InfisicalCredentialManager exactly
    # ------------------------------------------------------------------

    async def save_credentials(
        self,
        integration_id: str,
        envelope: CrmCredentialEnvelope,
    ) -> None:
        """Async version of ``InfisicalCredentialManager.save_credentials``."""
        await self._run(
            self._sync_manager.save_credentials,  # type: ignore[union-attr]
            integration_id,
            envelope,
        )

    async def get_credentials(
        self, integration_id: str
    ) -> CrmCredentialEnvelope:
        """Async version of ``InfisicalCredentialManager.get_credentials``."""
        return await self._run(
            self._sync_manager.get_credentials,  # type: ignore[union-attr]
            integration_id,
        )

    async def delete_credentials(self, integration_id: str) -> None:
        """Async version of ``InfisicalCredentialManager.delete_credentials``."""
        await self._run(
            self._sync_manager.delete_credentials,  # type: ignore[union-attr]
            integration_id,
        )

    async def rotate_credentials(
        self,
        integration_id: str,
        new_envelope: CrmCredentialEnvelope,
    ) -> CrmCredentialEnvelope:
        """Async version of ``InfisicalCredentialManager.rotate_credentials``."""
        return await self._run(
            self._sync_manager.rotate_credentials,  # type: ignore[union-attr]
            integration_id,
            new_envelope,
        )

    async def credentials_exist(self, integration_id: str) -> bool:
        """Async version of ``InfisicalCredentialManager.credentials_exist``."""
        return await self._run(
            self._sync_manager.credentials_exist,  # type: ignore[union-attr]
            integration_id,
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
                "AsyncInfisicalCredentialManager is not initialised. "
                "Use `await AsyncInfisicalCredentialManager.create()` "
                "or call `await instance._initialise()` first."
            )