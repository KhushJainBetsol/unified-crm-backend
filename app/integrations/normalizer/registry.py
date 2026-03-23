"""
app/integrations/normalizer/registry.py

Single entry point for ticket normalisation.

Instead of importing individual normalizers throughout the codebase,
the sync service calls normalize_ticket() or normalize_tickets() and
passes the source system name. The registry dispatches to the correct
normalizer automatically.

Usage:
    from app.integrations.normalizer.registry import normalize_ticket, normalize_tickets

    # single ticket
    ticket = normalize_ticket(raw_payload, source_system="zammad")

    # batch
    tickets = normalize_tickets(raw_list, source_system="espocrm")
"""

from __future__ import annotations

import logging
from typing import Callable

from app.integrations.normalizer.espo_normalizer import normalize_espo_ticket, normalize_espo_tickets
from app.integrations.normalizer.schema import NormalizedTicket
from app.integrations.normalizer.zammad_normalizer import normalize_zammad_ticket, normalize_zammad_tickets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry maps source system name → normalizer functions
# To add a new CRM: create its normalizer file and add an entry here.
# ---------------------------------------------------------------------------
_SINGLE_NORMALIZERS: dict[str, Callable[[dict], NormalizedTicket]] = {
    "zammad": normalize_zammad_ticket,
    "espocrm": normalize_espo_ticket,
}

_BATCH_NORMALIZERS: dict[str, Callable[[list[dict]], list[NormalizedTicket]]] = {
    "zammad": normalize_zammad_tickets,
    "espocrm": normalize_espo_tickets,
}


def normalize_ticket(raw: dict, source_system: str) -> NormalizedTicket:
    """
    Normalize a single raw CRM ticket dict into a NormalizedTicket.

    Args:
        raw:           Raw ticket dict from the CRM API.
        source_system: Source system name — must match a key in source_systems table
                       e.g. "zammad" or "espocrm".

    Returns:
        NormalizedTicket

    Raises:
        ValueError: if source_system is not registered.
        KeyError / ValueError: if required fields are missing in raw.
    """
    normalizer = _SINGLE_NORMALIZERS.get(source_system.lower())
    if normalizer is None:
        raise ValueError(
            f"No normalizer registered for source system {source_system!r}. "
            f"Available: {list(_SINGLE_NORMALIZERS.keys())}"
        )
    return normalizer(raw)


def normalize_tickets(raw_list: list[dict], source_system: str) -> list[NormalizedTicket]:
    """
    Normalize a batch of raw CRM ticket dicts.
    Skips failed tickets and logs errors — never raises on partial failure.

    Args:
        raw_list:      List of raw ticket dicts from the CRM API.
        source_system: Source system name e.g. "zammad" or "espocrm".

    Returns:
        List of successfully normalised NormalizedTicket objects.

    Raises:
        ValueError: if source_system is not registered.
    """
    normalizer = _BATCH_NORMALIZERS.get(source_system.lower())
    if normalizer is None:
        raise ValueError(
            f"No batch normalizer registered for source system {source_system!r}. "
            f"Available: {list(_BATCH_NORMALIZERS.keys())}"
        )
    results = normalizer(raw_list)
    logger.info(
        "Normalised %d/%d tickets from %s",
        len(results),
        len(raw_list),
        source_system,
    )
    return results


def get_supported_sources() -> list[str]:
    """Return all registered source system names."""
    return list(_SINGLE_NORMALIZERS.keys())
