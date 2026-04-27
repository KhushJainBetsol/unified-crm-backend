# app/factory/adapter_factory.py
"""
CrmAdapterFactory
=================
Dynamically constructs fully configured CRM adapters.

Change: create() now reads crm_org_id from the credentials envelope and
  passes it as a keyword argument to the adapter constructor.  This lets
  EspoCrmAdapter.fetch_agents() scope the Contact cross-match to the right
  Account without any extra DB lookups at fetch time.

  The crm_org_id field is expected on the credentials envelope
  (AsyncDbBackedCredentialService returns it alongside base_url and
  credentials).  If absent it defaults to None — adapters that don't need
  account scoping (e.g. Zammad) are unaffected.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, List, Optional, Type

from app.adapters.base.adapter import BaseCrmAdapter
from app.adapters.base.client import BaseCrmClient
from app.config.registry import AdapterRegistry
from app.credentials.db_credential_service import AsyncDbBackedCredentialService

logger = logging.getLogger(__name__)


class AdapterFactoryError(Exception):
    """Raised when the factory fails to construct an adapter."""


class CrmAdapterFactory:
    """
    Async factory that dynamically constructs CRM adapters.

    Parameters
    ----------
    registry:
        Pre-warmed AdapterRegistry containing all adapter configurations.
    credential_manager:
        AsyncDbBackedCredentialService — decrypts credentials from PostgreSQL
        using the AES key fetched from Infisical.
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        credential_manager: AsyncDbBackedCredentialService,
    ) -> None:
        self._registry     = registry
        self._cred_manager = credential_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        integration_id: str,
        required_capabilities: Optional[List[str]] = None,
    ) -> BaseCrmAdapter:
        """
        Build a fully injected, un-opened CRM adapter.

        The caller MUST open the adapter before use:

            adapter = await factory.create("uuid-1234")
            async with adapter:
                tickets = await adapter.fetch_tickets()

        Parameters
        ----------
        integration_id:
            The tenant's integration UUID (maps to crm_integrations.id).
        required_capabilities:
            Optional list of capability strings to validate against the
            registry before constructing the adapter.

        Returns
        -------
        BaseCrmAdapter
            Un-opened adapter instance with crm_org_id injected.

        Raises
        ------
        AdapterFactoryError
            If credentials can't be fetched, the config is missing,
            the class can't be imported, or instantiation fails.
        """
        logger.debug("Building adapter for integration_id='%s'", integration_id)

        # ── 1. Fetch credentials (async — DB + Infisical) ─────────────────
        try:
            envelope = await self._cred_manager.get_credentials(integration_id)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Failed to fetch credentials for integration '{integration_id}': {exc}"
            ) from exc

        crm_type   = envelope.crm_type
        # crm_org_id is stored on TenantSourceSystem and included in the
        # envelope by AsyncDbBackedCredentialService.  It may be None for
        # CRM types that don't need account scoping (e.g. Zammad).
        crm_org_id: Optional[str] = getattr(envelope, "crm_org_id", None)

        # ── 2. Fetch registry entry + config ──────────────────────────────
        try:
            entry  = self._registry.get_entry(crm_type)
            config = self._registry.get_adapter_config(crm_type)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Configuration missing or invalid for CRM type '{crm_type}': {exc}"
            ) from exc

        # ── 3. Optional capability check ──────────────────────────────────
        if required_capabilities:
            for cap in required_capabilities:
                if not self._registry.has_capability(crm_type, cap):
                    raise AdapterFactoryError(
                        f"CRM type '{crm_type}' does not support capability '{cap}'."
                    )

        # ── 4. Dynamically load the adapter class ─────────────────────────
        adapter_cls = self._import_class(entry.adapter_class)

        # ── 5. Resolve client class ───────────────────────────────────────
        client_cls: Type[BaseCrmClient] = getattr(
            adapter_cls, "client_class", BaseCrmClient
        )

        # ── 6. Build the dependency graph ─────────────────────────────────
        try:
            client = client_cls(
                base_url    = envelope.base_url,
                config      = config,
                credentials = envelope.credentials,
            )
            adapter = adapter_cls(
                client         = client,
                config         = config,
                integration_id = integration_id,
                crm_org_id     = crm_org_id,   # ← NEW: scopes agent fetching
            )
        except Exception as exc:
            raise AdapterFactoryError(
                f"Failed to instantiate {adapter_cls.__name__} "
                f"for '{integration_id}': {exc}"
            ) from exc

        logger.info(
            "Constructed %s for integration_id='%s' crm_org_id=%r",
            adapter_cls.__name__,
            integration_id,
            crm_org_id,
        )
        return adapter

    def clear_class_cache(self) -> None:
        """No-op kept for interface compatibility."""
        logger.debug("clear_class_cache() called — nothing to clear.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _import_class(self, class_path: str) -> Type[BaseCrmAdapter]:
        """Dynamically import a Python class from a dot-notation string."""
        try:
            module_path, class_name = class_path.rsplit(".", 1)
        except ValueError as exc:
            raise AdapterFactoryError(
                f"Invalid adapter_class path '{class_path}'. "
                "Must be a fully qualified dot-notation string."
            ) from exc

        try:
            module = importlib.import_module(module_path)
            cls    = getattr(module, class_name)
        except ImportError as exc:
            raise AdapterFactoryError(
                f"Failed to import module '{module_path}' "
                f"for adapter '{class_name}': {exc}"
            ) from exc
        except AttributeError as exc:
            raise AdapterFactoryError(
                f"Class '{class_name}' not found in module '{module_path}': {exc}"
            ) from exc

        if not issubclass(cls, BaseCrmAdapter):
            raise AdapterFactoryError(
                f"'{class_name}' must inherit from BaseCrmAdapter."
            )
        return cls