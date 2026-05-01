"""
app/integrations/normalizer/normalizer.py

Config-driven, CRM-agnostic ticket normalizer.

Instead of a separate espo_normalizer.py and zammad_normalizer.py, this single
module reads the CRM's AdapterConfig (loaded from config/<crm>/config.yaml) and
applies the field_mappings, status_map, and priority_map declared there.

For Zammad's dual priority format (integer priority_id on list endpoint vs
string on single-ticket endpoint) the normalizer uses the same resolution
logic as before — but now it reads the priority_map from the YAML config
instead of a separate TOML file.

Usage (unchanged public interface):
    from app.integrations.normalizer import normalize_ticket, normalize_tickets

    ticket  = normalize_ticket(raw, source_system="zammad", config=adapter_config)
    tickets = normalize_tickets(raws, source_system="espocrm", config=adapter_config)

The registry.py keeps the same signature so all callers continue to work
without modification — it just passes the AdapterConfig through.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.config.models import AdapterConfig
from app.integrations.normalizer.schema import NormalizedTicket

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "No Title"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        logger.warning("Could not parse datetime value: %r", value)
        return None


def _get_nested(obj: dict, path: str) -> object:
    """
    Resolve a dot-notation path against a dict.
    A leading '?' means optional — returns None instead of raising on missing key.
    """
    optional = path.startswith("?")
    clean_path = path.lstrip("?")

    try:
        result = obj
        for part in clean_path.split("."):
            if isinstance(result, dict):
                result = result[part]
            else:
                if optional:
                    return None
                raise KeyError(part)
        return result
    except (KeyError, TypeError):
        if optional:
            return None
        raise


def _map_field(raw: dict, path: str) -> object:
    """Resolve a field mapping path; optional paths return None on miss."""
    return _get_nested(raw, path)


# ---------------------------------------------------------------------------
# Priority resolution
# ---------------------------------------------------------------------------

def _resolve_priority_zammad(raw: dict, status_map: dict[str, str]) -> str | None:
    """
    Zammad has two priority formats:
      - List endpoint:   priority_id = 2  (integer)
      - Single endpoint: priority = "2 normal" or priority = 2

    The YAML priority_map keys are the *string* priority names
    (e.g. "2 normal", "1 low").  We also accept numeric keys for the
    priority_id case by converting them: 2 → "2".

    Falls back to None if nothing matches.
    """
    priority_map = status_map  # caller passes config.priority_map

    # Format 1: integer priority_id
    priority_id = raw.get("priority_id")
    if priority_id is not None:
        try:
            key = str(int(priority_id))
            result = priority_map.get(key)
            if result:
                return result
        except (ValueError, TypeError):
            pass

    # Format 2/3: priority field (string or int)
    priority = raw.get("priority")
    if priority is None:
        return None

    if isinstance(priority, int):
        result = priority_map.get(str(priority))
        if result:
            return result

    if isinstance(priority, str):
        normalized = priority.lower().strip()
        result = priority_map.get(normalized)
        if result:
            return result
        # Last attempt: extract numeric prefix "2 normal" → "2"
        parts = normalized.split()
        if parts and parts[0].isdigit():
            result = priority_map.get(parts[0])
            if result:
                return result

    logger.warning(
        "Could not resolve Zammad priority from: priority_id=%r priority=%r",
        raw.get("priority_id"), raw.get("priority"),
    )
    return None


# ---------------------------------------------------------------------------
# Core normalizer
# ---------------------------------------------------------------------------

def normalize_ticket(
    raw: dict,
    source_system: str,
    config: AdapterConfig,
) -> NormalizedTicket:
    """
    Normalize a single raw CRM ticket dict into a NormalizedTicket.

    All field paths, status mappings, and priority mappings are read
    from *config* (the CRM's AdapterConfig loaded from its YAML file).
    
    For webhook updates, many fields may be missing. This function provides
    graceful fallbacks:
      - Missing timestamps default to current UTC datetime
      - Missing title defaults to "No Title"
      - Missing status defaults to "open"
      - Missing relations (agent/customer/company) default to None

    Args:
        raw:           Raw ticket dict from the CRM API.
        source_system: CRM key, e.g. "zammad" or "espocrm".
        config:        Fully validated AdapterConfig for this CRM.

    Returns:
        NormalizedTicket

    Raises:
        KeyError / ValueError: if REQUIRED fields (id) are missing in raw.
    """
    mappings = config.field_mappings.ticket

    # ── ID (required) ─────────────────────────────────────────────────
    crm_ticket_id = str(_map_field(raw, mappings["id"]))

    # ── Timestamps ────────────────────────────────────────────────────
    # For webhook updates, timestamps may be missing. Use current UTC as fallback.
    created_at_raw = _map_field(raw, mappings.get("created_at", "?created_at"))
    updated_at_raw = _map_field(raw, mappings.get("updated_at", "?updated_at"))
    
    created_at = _parse_datetime(str(created_at_raw)) if created_at_raw else datetime.now(timezone.utc)
    updated_at = _parse_datetime(str(updated_at_raw)) if updated_at_raw else datetime.now(timezone.utc)

    # ── Title ─────────────────────────────────────────────────────────
    title_path = mappings.get("title", "?title")
    title_raw = _map_field(raw, title_path)
    title = str(title_raw or DEFAULT_TITLE).strip() or DEFAULT_TITLE

    # ── Description ───────────────────────────────────────────────────
    description = None
    if "description" in mappings:
        description = _map_field(raw, mappings["description"]) or None
        if description is not None:
            description = str(description)

    # Zammad uses 'note' or 'body' as description if not mapped
    if description is None and source_system.lower() == "zammad":
        description = raw.get("note") or raw.get("body") or None

    # ── Status ────────────────────────────────────────────────────────
    raw_status = ""
    if "status" in mappings:
        status_raw = _map_field(raw, mappings["status"])
        raw_status = str(status_raw or "").lower().strip() if status_raw else ""

    status = config.status_map.get(raw_status, "open")
    if raw_status and raw_status not in config.status_map:
        logger.warning(
            "Unknown %s status %r for ticket %s — using fallback 'open'",
            source_system, raw_status, crm_ticket_id,
        )

    # ── Priority ──────────────────────────────────────────────────────
    if source_system.lower() == "zammad":
        priority = _resolve_priority_zammad(raw, config.priority_map)
    else:
        raw_priority = ""
        if "priority" in mappings:
            raw_priority = str(_map_field(raw, mappings["priority"]) or "").lower().strip()
        priority = config.priority_map.get(raw_priority) or None
        if raw_priority and raw_priority not in config.priority_map:
            logger.warning(
                "Unknown %s priority %r for ticket %s — priority will be None",
                source_system, raw_priority, crm_ticket_id,
            )

    # Empty-string priority → None
    if not priority:
        priority = None

    # ── Related entity IDs ────────────────────────────────────────────
    crm_agent_id = None
    if "assignee_id" in mappings:
        val = _map_field(raw, mappings["assignee_id"])
        crm_agent_id = str(val) if val else None

    crm_customer_id = None
    if "customer_id" in mappings:
        val = _map_field(raw, mappings["customer_id"])
        crm_customer_id = str(val) if val else None

    crm_company_id = None
    if "organization_id" in mappings:
        val = _map_field(raw, mappings["organization_id"])
        crm_company_id = str(val) if val else None

    # ── closed_at ─────────────────────────────────────────────────────
    closed_at: datetime | None = None
    if source_system.lower() == "zammad":
        closed_at = _parse_datetime(raw.get("close_at"))
    elif status == "closed":
        # EspoCRM has no dedicated close timestamp — use updated_at as proxy
        closed_at = updated_at

    return NormalizedTicket(
        crm_ticket_id=crm_ticket_id,
        source_system=source_system,
        title=title,
        description=description,
        status=status,
        priority=priority,
        crm_agent_id=crm_agent_id,
        crm_customer_id=crm_customer_id,
        crm_company_id=crm_company_id,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=closed_at,
    )


def normalize_tickets(
    raw_list: list[dict],
    source_system: str,
    config: AdapterConfig,
) -> list[NormalizedTicket]:
    """
    Normalize a batch of raw CRM ticket dicts.

    Skips tickets that fail normalization and logs errors — never raises
    on partial failure.

    Args:
        raw_list:      List of raw ticket dicts from the CRM API.
        source_system: CRM key, e.g. "zammad" or "espocrm".
        config:        Fully validated AdapterConfig for this CRM.

    Returns:
        List of successfully normalised NormalizedTicket objects.
    """
    results: list[NormalizedTicket] = []
    for raw in raw_list:
        try:
            results.append(normalize_ticket(raw, source_system, config))
        except (KeyError, ValueError) as exc:
            logger.error(
                "Failed to normalize %s ticket id=%r: %s",
                source_system, raw.get("id"), exc,
            )
    logger.info(
        "Normalised %d/%d tickets from %s",
        len(results), len(raw_list), source_system,
    )
    return results
