"""
app/integrations/normalizer/espo_normalizer.py

Converts raw EspoCRM API Case payload → NormalizedTicket.

EspoCRM quirks handled:
  - "name"             → title       (EspoCRM calls it "name" not "title")
  - "modifiedAt"       → updated_at  (not "updated_at")
  - "assignedUserId"   → crm_agent_id
  - "contactId"        → crm_customer_id
  - "accountId"        → crm_company_id
  - "id" is a string UUID in EspoCRM
  - status values are Title Case ("New", "In Process") — normalised here
  - no dedicated close timestamp — derived from status

EspoCRM API reference:
  GET /api/v1/Case/:id
  GET /api/v1/Case  (with pagination via offset/maxSize)
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.integrations.normalizer.schema import NormalizedTicket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status mapping
# EspoCRM case status → your standard status value
# ---------------------------------------------------------------------------
ESPO_STATUS_MAP: dict[str, str] = {
    "new": "open",
    "assigned": "open",
    "in process": "pending",
    "pending": "pending",
    "closed": "closed",
    "rejected": "closed",
    "duplicate": "closed",
}

# ---------------------------------------------------------------------------
# Priority mapping
# EspoCRM uses Title Case — normalise to lowercase first before lookup
# ---------------------------------------------------------------------------
ESPO_PRIORITY_MAP: dict[str, str] = {
    "low": "low",
    "normal": "normal",
    "high": "high",
    "urgent": "urgent",
}

DEFAULT_STATUS = "open"
DEFAULT_TITLE = "No Title"


def _parse_datetime(value: str | None) -> datetime | None:
    """
    Safely parse an ISO 8601 datetime string.
    Returns None instead of raising if value is missing or malformed.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        logger.warning("Could not parse datetime value: %r", value)
        return None


def _derive_closed_at(status: str, modified_at: datetime) -> datetime | None:
    """
    EspoCRM Cases don't have a dedicated close timestamp.
    If the status is 'closed' we use the last modified timestamp as a proxy.
    """
    return modified_at if status == "closed" else None


def normalize_espo_ticket(raw: dict) -> NormalizedTicket:
    """
    Convert a single raw EspoCRM Case dict into a NormalizedTicket.

    Args:
        raw: Raw case dict from the EspoCRM REST API.

    Returns:
        NormalizedTicket with all fields normalised to your standard format.

    Raises:
        KeyError:  if required fields (id, createdAt, modifiedAt) are missing.
        ValueError: if datetime parsing fails for required timestamp fields.
    """
    # ---- required fields ----
    crm_ticket_id = str(raw["id"])
    created_at = datetime.fromisoformat(raw["createdAt"])
    updated_at = datetime.fromisoformat(raw["modifiedAt"])

    # ---- status ----
    raw_status = str(raw.get("status", "")).lower().strip()
    status = ESPO_STATUS_MAP.get(raw_status, DEFAULT_STATUS)
    if raw_status and raw_status not in ESPO_STATUS_MAP:
        logger.warning(
            "Unknown EspoCRM status %r for case %s — defaulting to %r",
            raw_status,
            crm_ticket_id,
            DEFAULT_STATUS,
        )

    # ---- priority ----
    raw_priority = str(raw.get("priority", "")).lower().strip()
    priority = ESPO_PRIORITY_MAP.get(raw_priority)
    if raw_priority and priority is None:
        logger.warning(
            "Unknown EspoCRM priority %r for case %s — setting to None",
            raw_priority,
            crm_ticket_id,
        )

    return NormalizedTicket(
        crm_ticket_id=crm_ticket_id,
        source_system="espocrm",
        title=(raw.get("name") or DEFAULT_TITLE).strip(),
        description=raw.get("description") or None,
        status=status,
        priority=priority,
        crm_agent_id=raw.get("assignedUserId") or None,
        crm_customer_id=raw.get("contactId") or None,
        crm_company_id=raw.get("accountId") or None,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=_derive_closed_at(status, updated_at),
    )


def normalize_espo_tickets(raw_list: list[dict]) -> list[NormalizedTicket]:
    """
    Normalise a list of raw EspoCRM case dicts.
    Skips any case that fails normalisation and logs the error.
    """
    results: list[NormalizedTicket] = []
    for raw in raw_list:
        try:
            results.append(normalize_espo_ticket(raw))
        except (KeyError, ValueError) as exc:
            logger.error(
                "Failed to normalize EspoCRM case id=%r: %s",
                raw.get("id"),
                exc,
            )
    return results
