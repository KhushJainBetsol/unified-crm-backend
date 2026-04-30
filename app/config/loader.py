# crm/config/loader.py
"""
ConfigLoader
============
Responsible for reading YAML files from disk, validating them against the
Pydantic schema models, and returning strongly-typed configuration objects.

Design decisions:
- Caches parsed configs keyed by their file path so repeated access is O(1).
- Raises descriptive errors on validation failure so misconfiguration is caught
  at startup, not mid-request.
- Pure sync I/O — configs are loaded once at boot, so async is unnecessary.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import yaml
from pydantic import ValidationError

from app.config.models import AdapterConfig, AdapterRegistryManifest

logger = logging.getLogger(__name__)


class ConfigLoaderError(Exception):
    """Raised when a config file cannot be read or fails validation."""


class ConfigLoader:
    """
    Loads and validates YAML configuration files.

    Usage
    -----
    loader = ConfigLoader(base_dir=Path("config"))
    manifest = loader.load_registry_manifest("crm_adapters.yaml")
    zammad_cfg = loader.load_adapter_config("zammad/config.yaml")
    """

    def __init__(self, base_dir: Path) -> None:
        """
        Parameters
        ----------
        base_dir:
            Root directory that all config paths are resolved relative to.
        """
        self._base_dir = base_dir.resolve()
        self._adapter_config_cache: Dict[str, AdapterConfig] = {}
        self._manifest_cache: Optional[AdapterRegistryManifest] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_registry_manifest(
        self, manifest_filename: str = "crm_adapters.yaml"
    ) -> AdapterRegistryManifest:
        """
        Parse and validate the master adapter registry (crm_adapters.yaml).

        Returns a cached result on subsequent calls.
        """
        if self._manifest_cache is not None:
            return self._manifest_cache

        raw = self._read_yaml(manifest_filename)
        try:
            manifest = AdapterRegistryManifest.model_validate(raw)
        except ValidationError as exc:
            raise ConfigLoaderError(
                f"Registry manifest '{manifest_filename}' failed validation:\n{exc}"
            ) from exc

        logger.info(
            "Loaded adapter registry: %d adapters found — %s",
            len(manifest.adapters),
            list(manifest.adapters.keys()),
        )
        self._manifest_cache = manifest
        return manifest

    def load_adapter_config(self, config_path: str) -> AdapterConfig:
        """
        Parse and validate a single adapter's YAML configuration file.

        Parameters
        ----------
        config_path:
            Path relative to *base_dir*, e.g. ``"zammad/config.yaml"``.

        Returns a cached result on subsequent calls with the same path.
        """
        if config_path in self._adapter_config_cache:
            return self._adapter_config_cache[config_path]

        raw = self._read_yaml(config_path)
        try:
            config = AdapterConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigLoaderError(
                f"Adapter config '{config_path}' failed validation:\n{exc}"
            ) from exc

        logger.info("Loaded adapter config from '%s'.", config_path)
        self._adapter_config_cache[config_path] = config
        return config

    def invalidate_cache(self, config_path: Optional[str] = None) -> None:
        """
        Clear the in-memory cache.

        Parameters
        ----------
        config_path:
            If given, only evict that single entry.  If None, flush everything.
        """
        if config_path:
            self._adapter_config_cache.pop(config_path, None)
        else:
            self._adapter_config_cache.clear()
            self._manifest_cache = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_yaml(self, relative_path: str) -> dict:
        """Read and parse a YAML file, returning a raw dict."""
        full_path = self._base_dir / relative_path
        if not full_path.exists():
            raise ConfigLoaderError(
                f"Config file not found: '{full_path}'. "
                f"Check that base_dir='{self._base_dir}' is correct."
            )
        try:
            with full_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigLoaderError(
                f"YAML parse error in '{full_path}': {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ConfigLoaderError(
                f"Expected a YAML mapping at the root of '{full_path}', "
                f"got {type(data).__name__}."
            )
        return data