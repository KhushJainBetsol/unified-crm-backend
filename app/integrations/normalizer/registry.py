"""
app/integrations/normalizer/registry.py

Config-driven normalizer registry.

Single entry point for ticket normalisation.  Instead of a hard-coded
dispatch table that maps source-system names to CRM-specific functions,
this registry delegates to the single config-driven normalizer and
fetches the AdapterConfig from the AdapterRegistry.

This means: to add a new CRM you only add its YAML config file and
register it in crm_adapters.yaml.  No Python changes needed here.

Usage (unchanged public API):
    from app.integrations.normalizer.registry import normalize_ticket, normalize_tickets

    ticket  = normalize_ticket(raw, source_system="zammad")
    tickets = normalize_tickets(raw_list, source_system="espocrm")
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config.registry import AdapterNotFoundError, AdapterRegistry
from app.integrations.normalizer.normalizer import (
    normalize_ticket as _normalize_one,
    normalize_tickets as _normalize_many,
)
from app.integrations.normalizer.schema import NormalizedTicket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level AdapterRegistry (lazy-initialised once)
# ---------------------------------------------------------------------------
# We keep a module-level instance so the registry is initialised once per
# process.  Routes and services that already have an AdapterRegistry on
# app.state can call normalize_ticket_with_config() directly if they prefer.
# ---------------------------------------------------------------------------

_registry: AdapterRegistry | None = None


def _get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        from app.core.settings import get_settings
        settings = get_settings()
        config_dir = Path(settings.CRM_CONFIG_DIR)  # Use CRM_CONFIG_DIR from settings
        _registry = AdapterRegistry(config_base_dir=config_dir)
        _registry.initialise()
    return _registry


# ---------------------------------------------------------------------------
# Public API  (same signatures as before)
# ---------------------------------------------------------------------------

def normalize_ticket(raw: dict, source_system: str) -> NormalizedTicket:
    """
    Normalize a single raw CRM ticket dict into a NormalizedTicket.

    Reads field mappings and value maps from the CRM's YAML config.

    Args:
        raw:           Raw ticket dict from the CRM API.
        source_system: CRM key — must match a registered adapter key
                       (e.g. "zammad" or "espocrm").

    Returns:
        NormalizedTicket

    Raises:
        ValueError:    if source_system is not registered.
        KeyError:      if required fields are missing in raw.
    """
    config = _get_adapter_config(source_system)
    return _normalize_one(raw, source_system=source_system, config=config)


def normalize_tickets(
    raw_list: list[dict], source_system: str
) -> list[NormalizedTicket]:
    """
    Normalize a batch of raw CRM ticket dicts.

    Skips failed tickets and logs errors — never raises on partial failure.

    Args:
        raw_list:      List of raw ticket dicts from the CRM API.
        source_system: CRM key e.g. "zammad" or "espocrm".

    Returns:
        List of successfully normalised NormalizedTicket objects.

    Raises:
        ValueError: if source_system is not registered.
    """
    config = _get_adapter_config(source_system)
    results = _normalize_many(raw_list, source_system=source_system, config=config)
    logger.info(
        "Normalised %d/%d tickets from %s",
        len(results), len(raw_list), source_system,
    )
    return results


def get_supported_sources() -> list[str]:
    """Return all registered adapter keys (source system names)."""
    return _get_registry().list_adapter_keys()


# ---------------------------------------------------------------------------
# Extended public API — for callers that already hold an AdapterRegistry
# ---------------------------------------------------------------------------

def normalize_ticket_with_registry(
    raw: dict,
    source_system: str,
    registry: AdapterRegistry,
) -> NormalizedTicket:
    """
    Normalize a ticket using a caller-supplied registry (e.g. from app.state).
    Avoids the module-level lazy-init path.
    """
    try:
        config = registry.get_adapter_config(source_system)
    except AdapterNotFoundError:
        raise ValueError(
            f"No adapter registered for source system {source_system!r}. "
            f"Available: {registry.list_adapter_keys()}"
        )
    return _normalize_one(raw, source_system=source_system, config=config)


def normalize_tickets_with_registry(
    raw_list: list[dict],
    source_system: str,
    registry: AdapterRegistry,
) -> list[NormalizedTicket]:
    """
    Normalize a batch using a caller-supplied registry.
    """
    try:
        config = registry.get_adapter_config(source_system)
    except AdapterNotFoundError:
        raise ValueError(
            f"No adapter registered for source system {source_system!r}. "
            f"Available: {registry.list_adapter_keys()}"
        )
    results = _normalize_many(raw_list, source_system=source_system, config=config)
    logger.info(
        "Normalised %d/%d tickets from %s",
        len(results), len(raw_list), source_system,
    )
    return results


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _get_adapter_config(source_system: str):
    registry = _get_registry()
    try:
        return registry.get_adapter_config(source_system.lower())
    except AdapterNotFoundError:
        raise ValueError(
            f"No normalizer config for source system {source_system!r}. "
            f"Available: {registry.list_adapter_keys()}"
        )
