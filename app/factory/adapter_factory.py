"""
CrmAdapterFactory
=================
The engine that dynamically constructs fully configured CRM adapters.

Design decisions
----------------
- Inversion of Control (IoC): The adapter does not fetch its own credentials
  nor does it build its own HTTP client. The factory builds the entire dependency
  graph and injects it.
- Dynamic Imports: Uses `importlib` to load adapter classes at runtime based on
  the string path in `crm_adapters.yaml`. This prevents massive circular imports
  and `if/elif` blocks.
- Separation of Lifecycle: The factory ONLY constructs the objects in memory. 
  It does NOT call `await adapter.open()`. The service layer is responsible for
  managing the connection lifecycle using `async with`.
"""

import importlib
import logging
from typing import Any, Type

from app.adapters.base.adapter import BaseCrmAdapter
from app.adapters.base.client import BaseCrmClient
from app.config.registry import AdapterRegistry
from app.credentials.manager import InfisicalCredentialManager

logger = logging.getLogger(__name__)


class AdapterFactoryError(Exception):
    """Raised when the factory fails to construct an adapter."""


class CrmAdapterFactory:
    """
    Dynamically constructs CRM adapters based on the integration ID.

    Parameters
    ----------
    registry : AdapterRegistry
        The pre-warmed registry containing all adapter configurations.
    credential_manager : InfisicalCredentialManager
        The secure vault manager for fetching runtime secrets.
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        credential_manager: InfisicalCredentialManager,
    ) -> None:
        self._registry = registry
        self._cred_manager = credential_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, integration_id: str) -> BaseCrmAdapter:
        """
        Build a fully injected CRM adapter for the given integration.

        Parameters
        ----------
        integration_id : str
            The tenant's integration reference UUID.

        Returns
        -------
        BaseCrmAdapter
            An un-opened adapter instance. The caller MUST use it as an
            async context manager to establish the connection.

        Usage
        -----
        adapter = factory.create("uuid-1234")
        async with adapter:
            tickets = await adapter.fetch_tickets()
        """
        logger.debug("Building adapter for integration_id='%s'", integration_id)

        # 1. Fetch credentials (this dictates the CRM type)
        try:
            envelope = self._cred_manager.get_credentials(integration_id)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Failed to fetch credentials for integration '{integration_id}': {exc}"
            ) from exc

        crm_type = envelope.crm_type

        # 2. Fetch the rules (Registry Entry & Config)
        try:
            entry = self._registry.get_entry(crm_type)
            config = self._registry.get_adapter_config(crm_type)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Configuration missing or invalid for CRM type '{crm_type}': {exc}"
            ) from exc

        # 3. Dynamically load the Adapter Class
        adapter_cls = self._import_class(entry.adapter_class)

        # 4. Determine the Client Class
        # We look for a `client_class` attribute on the Adapter class.
        # If the adapter doesn't declare a custom client, we use the BaseCrmClient.
        client_cls: Type[BaseCrmClient] = getattr(adapter_cls, "client_class", BaseCrmClient)

        # 5. Build the object graph (Dependency Injection)
        try:
            # Instantiate the HTTP Engine
            client = client_cls(
                base_url=envelope.base_url,
                config=config,
                credentials=envelope.credentials,
            )

            # Instantiate the Adapter, injecting the client
            adapter = adapter_cls(
                client=client,
                config=config,
                integration_id=integration_id,
            )
        except Exception as exc:
            raise AdapterFactoryError(
                f"Failed to instantiate {adapter_cls.__name__} for '{integration_id}': {exc}"
            ) from exc

        logger.info(
            "Successfully constructed %s for integration_id='%s'",
            adapter_cls.__name__,
            integration_id,
        )
        return adapter

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _import_class(self, class_path: str) -> Type[BaseCrmAdapter]:
        """
        Dynamically import a Python class from a dot-notation string.

        Example: "crm.adapters.zammad.adapter.ZammadAdapter"
        """
        try:
            module_path, class_name = class_path.rsplit(".", 1)
        except ValueError as exc:
            raise AdapterFactoryError(
                f"Invalid adapter_class path '{class_path}'. "
                "Must be a fully qualified dot-notation string."
            ) from exc

        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except ImportError as exc:
            raise AdapterFactoryError(
                f"Failed to import module '{module_path}' for adapter '{class_name}': {exc}"
            ) from exc
        except AttributeError as exc:
            raise AdapterFactoryError(
                f"Class '{class_name}' not found in module '{module_path}': {exc}"
            ) from exc

        # Type safety check at runtime
        if not issubclass(cls, BaseCrmAdapter):
            raise AdapterFactoryError(
                f"Class '{class_name}' must inherit from BaseCrmAdapter."
            )

        return cls