"""
app/integrations/normalizer/zammad_normalizer.py

Converts raw Zammad API ticket payload → NormalizedTicket.

Zammad priority quirk:
  The ticket list endpoint returns `priority_id` (int) not `priority`.
  The single ticket endpoint may return `priority` as string or int.
  Both are handled below.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.integrations.normalizer.schema import NormalizedTicket

logger = logging.getLogger(__name__)

ZAMMAD_STATUS_MAP: dict[str, str] = {
    "new": "open",
    "open": "open",
    "pending reminder": "pending",
    "pending close": "pending",
    "closed": "closed",
    "merged": "closed",
    "removed": "closed",
}

# Integer ID → standard priority (used when field is priority_id or int)
ZAMMAD_PRIORITY_ID_MAP: dict[int, str] = {
    1: "low",
    2: "normal",
    3: "high",
    4: "urgent",
}

# String → standard priority (used when field is a string)
ZAMMAD_PRIORITY_NAME_MAP: dict[str, str] = {
    "1 low": "low",
    "2 normal": "normal",
    "3 high": "high",
    "4 urgent": "urgent",
    "low": "low",
    "normal": "normal",
    "high": "high",
    "urgent": "urgent",
}

DEFAULT_STATUS = "open"
DEFAULT_TITLE = "No Title"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        logger.warning("Could not parse datetime value: %r", value)
        return None


def _resolve_priority(raw: dict) -> str | None:
    """
    Resolve priority from Zammad ticket dict.

    Zammad list endpoint returns:  priority_id = 2  (integer)
    Zammad single endpoint returns: priority = "2 normal" or priority = 2

    We check both keys so either endpoint works correctly.
    """
    # --- try priority_id first (list endpoint returns this) ---
    priority_id = raw.get("priority_id")
    if priority_id is not None:
        try:
            result = ZAMMAD_PRIORITY_ID_MAP.get(int(priority_id))
            if result:
                return result
        except (ValueError, TypeError):
            pass

    # --- try priority field (single ticket endpoint) ---
    priority = raw.get("priority")
    if priority is None:
        return None

    if isinstance(priority, int):
        return ZAMMAD_PRIORITY_ID_MAP.get(priority)

    if isinstance(priority, str):
        result = ZAMMAD_PRIORITY_NAME_MAP.get(priority.lower().strip())
        if result:
            return result
        # last attempt — extract numeric prefix e.g. "2 normal" → 2
        parts = priority.strip().split()
        if parts and parts[0].isdigit():
            return ZAMMAD_PRIORITY_ID_MAP.get(int(parts[0]))

    logger.warning("Could not resolve Zammad priority from raw: %r", raw.get("priority_id") or raw.get("priority"))
    return None


def normalize_zammad_ticket(raw: dict) -> NormalizedTicket:
    crm_ticket_id = str(raw["id"])
    created_at = datetime.fromisoformat(raw["created_at"])
    updated_at = datetime.fromisoformat(raw["updated_at"])

    # ---- status ----
    raw_state = str(raw.get("state", "")).lower().strip()
    status = ZAMMAD_STATUS_MAP.get(raw_state, DEFAULT_STATUS)
    if raw_state and raw_state not in ZAMMAD_STATUS_MAP:
        logger.warning(
            "Unknown Zammad state %r for ticket %s — defaulting to %r",
            raw_state, crm_ticket_id, DEFAULT_STATUS,
        )

    return NormalizedTicket(
        crm_ticket_id=crm_ticket_id,
        source_system="zammad",
        title=(raw.get("title") or DEFAULT_TITLE).strip(),
        description=raw.get("note") or raw.get("body") or None,
        status=status,
        priority=_resolve_priority(raw),
        crm_agent_id=str(raw["owner_id"]) if raw.get("owner_id") else None,
        crm_customer_id=str(raw["customer_id"]) if raw.get("customer_id") else None,
        crm_company_id=str(raw["organization_id"]) if raw.get("organization_id") else None,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=_parse_datetime(raw.get("close_at")),
    )


def normalize_zammad_tickets(raw_list: list[dict]) -> list[NormalizedTicket]:
    results: list[NormalizedTicket] = []
    for raw in raw_list:
        try:
            results.append(normalize_zammad_ticket(raw))
        except (KeyError, ValueError) as exc:
            logger.error("Failed to normalize Zammad ticket id=%r: %s", raw.get("id"), exc)
    return results