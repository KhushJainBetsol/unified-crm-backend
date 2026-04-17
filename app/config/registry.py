# crm/config/registry.py
"""
AdapterRegistry
===============
A singleton-style service that owns the *master* view of which CRM adapters
exist, what their capabilities are, and where to find their configs.

It deliberately does NOT instantiate adapters — that is the factory's job.
The registry only answers the question: "what do I know about adapter X?".

Responsibilities
----------------
- Load and hold the AdapterRegistryManifest (from crm_adapters.yaml).
- Provide O(1) look-ups of registry entries by adapter key.
- Pre-load and cache every adapter's AdapterConfig on first access.
- Validate that a requested capability is declared by an adapter before the
  factory wastes time instantiating it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from app.config.loader import ConfigLoader, ConfigLoaderError
from app.config.models import AdapterConfig, AdapterRegistryEntry, AdapterRegistryManifest

logger = logging.getLogger(__name__)


class AdapterNotFoundError(KeyError):
    """Raised when the registry has no entry for a requested adapter key."""


class CapabilityNotSupportedError(NotImplementedError):
    """Raised when an adapter does not declare a required capability."""


class AdapterRegistry:
    """
    Central catalogue of all registered CRM adapters.

    Parameters
    ----------
    config_base_dir:
        Root directory from which all config paths in the manifest are resolved.
    manifest_filename:
        Filename of the master registry YAML, relative to *config_base_dir*.

    Example
    -------
    >>> registry = AdapterRegistry(config_base_dir=Path("config"))
    >>> entry = registry.get_entry("zammad")
    >>> config = registry.get_adapter_config("zammad")
    """

    def __init__(
        self,
        config_base_dir: Path,
        manifest_filename: str = "crm_adapters.yaml",
    ) -> None:
        self._loader = ConfigLoader(base_dir=config_base_dir)
        self._manifest_filename = manifest_filename
        self._manifest: Optional[AdapterRegistryManifest] = None
        # Eager-populated cache of fully-loaded adapter configs
        self._adapter_configs: Dict[str, AdapterConfig] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialise(self) -> None:
        """
        Load the manifest and pre-warm the config cache for every adapter.
        Call this once at application startup (e.g. in a FastAPI lifespan hook).
        Idempotent — safe to call multiple times.
        """
        if self._manifest is not None:
            return  # already initialised

        self._manifest = self._loader.load_registry_manifest(self._manifest_filename)

        for key, entry in self._manifest.adapters.items():
            try:
                cfg = self._loader.load_adapter_config(entry.config_path)
                self._adapter_configs[key] = cfg
                logger.debug("Pre-loaded config for adapter '%s'.", key)
            except ConfigLoaderError:
                logger.exception(
                    "Failed to pre-load config for adapter '%s' — it will be "
                    "skipped.  Fix the YAML and restart.",
                    key,
                )

        logger.info(
            "AdapterRegistry initialised.  Available adapters: %s",
            self.list_adapter_keys(),
        )

    def _ensure_initialised(self) -> None:
        if self._manifest is None:
            raise RuntimeError(
                "AdapterRegistry.initialise() must be called before use."
            )

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def list_adapter_keys(self) -> List[str]:
        """Return all registered adapter keys (e.g. ['zammad', 'espocrm'])."""
        self._ensure_initialised()
        return list(self._manifest.adapters.keys())  # type: ignore[union-attr]

    def get_entry(self, adapter_key: str) -> AdapterRegistryEntry:
        """
        Return the registry manifest entry for *adapter_key*.

        Raises
        ------
        AdapterNotFoundError
            If no adapter is registered under that key.
        """
        self._ensure_initialised()
        try:
            return self._manifest.adapters[adapter_key]  # type: ignore[index]
        except KeyError:
            raise AdapterNotFoundError(
                f"No adapter registered for key '{adapter_key}'. "
                f"Available: {self.list_adapter_keys()}"
            )

    def get_adapter_config(self, adapter_key: str) -> AdapterConfig:
        """
        Return the fully-validated AdapterConfig for *adapter_key*.

        Raises
        ------
        AdapterNotFoundError
            If the adapter key is unknown.
        ConfigLoaderError
            If the config file exists but fails validation (lazy-load path).
        """
        self._ensure_initialised()
        if adapter_key not in self._adapter_configs:
            # Lazy-load path: adapter was skipped during initialisation or added
            # dynamically after boot.
            entry = self.get_entry(adapter_key)
            cfg = self._loader.load_adapter_config(entry.config_path)
            self._adapter_configs[adapter_key] = cfg

        return self._adapter_configs[adapter_key]

    def assert_capability(self, adapter_key: str, capability: str) -> None:
        """
        Assert that *adapter_key* declares *capability* in the manifest.

        Raises
        ------
        CapabilityNotSupportedError
            If the capability is absent from the adapter's declared set.
        """
        entry = self.get_entry(adapter_key)
        if capability not in entry.supported_capabilities:
            raise CapabilityNotSupportedError(
                f"Adapter '{adapter_key}' does not support capability "
                f"'{capability}'.  Supported: {entry.supported_capabilities}"
            )

    def supports_capability(self, adapter_key: str, capability: str) -> bool:
        """Boolean convenience wrapper around assert_capability."""
        try:
            self.assert_capability(adapter_key, capability)
            return True
        except CapabilityNotSupportedError:
            return False