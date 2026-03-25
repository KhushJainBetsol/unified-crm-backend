"""
app/integrations/normalizer/comment_normalizer.py

Converts raw CRM comment dicts into NormalizedComment objects.

Zammad  raw shape  (ticket_article):
    {
        "id": 12,
        "ticket_id": 6,
        "type": "note",
        "body": "This is the comment text",
        "from": "Agent Name <agent@example.com>",
        "created_by": "agent@example.com",
        "internal": false,
        "created_at": "2024-01-15T10:30:00.000Z",
        "updated_at": "2024-01-15T10:30:00.000Z"
    }

EspoCRM raw shape  (stream Post):
    {
        "id": "abc123def456",
        "type": "Post",
        "data": {
            "post": "This is the comment text"
        },
        "createdByName": "John Agent",
        "createdById": "user-uuid",
        "isInternal": false,
        "createdAt": "2024-01-15T10:30:00.000Z",
        "modifiedAt": "2024-01-15T10:30:00.000Z"
    }
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.integrations.normalizer.comment_schema import NormalizedComment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a timezone-aware datetime, or return None."""
    if not value:
        return None
    try:
        # Python 3.11+: fromisoformat handles Z suffix
        # Python 3.10 and below: replace Z manually
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        logger.warning("Could not parse datetime: %r", value)
        return None


def _extract_email(raw: str | None) -> str | None:
    """
    Pull email address out of strings like:
      "Agent Name <agent@example.com>"
      "agent@example.com"
    Returns None if no valid email found.
    """
    if not raw:
        return None
    match = re.search(r"<([^>]+)>", raw)
    if match:
        return match.group(1).strip()
    if "@" in raw:
        return raw.strip()
    return None


def _extract_name(raw: str | None) -> str | None:
    """
    Pull display name from "Agent Name <email@example.com>" format.
    Returns the whole string if no angle-bracket found.
    """
    if not raw:
        return None
    match = re.match(r"^(.+?)\s*<[^>]+>$", raw.strip())
    if match:
        return match.group(1).strip()
    # If it looks like just an email, return None for name
    if "@" in raw:
        return None
    return raw.strip()


# ---------------------------------------------------------------------------
# Zammad normalizer
# ---------------------------------------------------------------------------

def normalize_zammad_comment(raw: dict) -> NormalizedComment | None:
    """
    Convert a single Zammad ticket_article dict into a NormalizedComment.

    Returns None if the raw dict is missing the required 'id' field.
    """
    crm_id = raw.get("id")
    if crm_id is None:
        logger.warning("Zammad article missing 'id', skipping: %s", raw)
        return None

    from_field = raw.get("from") or raw.get("created_by") or ""
    author_name  = _extract_name(from_field)
    author_email = _extract_email(from_field)

    # Zammad: "internal": true means visible to agents only
    is_internal = bool(raw.get("internal", False))

    return NormalizedComment(
        crm_comment_id=str(crm_id),
        body=raw.get("body"),
        comment_type=raw.get("type"),
        author_name=author_name,
        author_email=author_email,
        is_internal=is_internal,
        crm_created_at=_parse_dt(raw.get("created_at")),
        crm_updated_at=_parse_dt(raw.get("updated_at")),
        source_system="zammad",
    )


def normalize_zammad_comments(raw_list: list[dict]) -> list[NormalizedComment]:
    """Normalize a list of Zammad articles, skipping any that fail."""
    results = []
    for raw in raw_list:
        if raw.get("type") != "note":
            continue
        comment = normalize_zammad_comment(raw)
        if comment:
            results.append(comment)
    return results

def extract_first_zammad_body(raw_list: list[dict]) -> str | None:
    """
    Sort ALL articles (any type) by created_at ascending,
    pick the very first one, and return its body.

    This is used to populate the ticket description with the
    original opening message before any type filtering is applied.

    Returns None if raw_list is empty or the first article has no body.
    """
    if not raw_list:
        return None

    sorted_articles = sorted(
        raw_list,
        key=lambda a: a.get("created_at") or "",  # empty string sorts before any date
    )

    first = sorted_articles[0]
    return first.get("body") or None


# ---------------------------------------------------------------------------
# EspoCRM normalizer
# ---------------------------------------------------------------------------

def normalize_espo_comment(raw: dict) -> NormalizedComment | None:
    """
    Convert a single EspoCRM stream Post dict into a NormalizedComment.

    Returns None if the raw dict is missing the required 'id' field.
    """
    crm_id = raw.get("id")
    if crm_id is None:
        logger.warning("EspoCRM stream item missing 'id', skipping: %s", raw)
        return None

    # Post body lives under data.post in EspoCRM stream responses
    data_block = raw.get("data") or {}
    body = data_block.get("post") or raw.get("post")

    author_name  = raw.get("createdByName")
    # EspoCRM stream items don't always include the author's email directly
    author_email = raw.get("createdByEmail")  # present in some configs

    # EspoCRM: isInternal flag (may not always be present)
    is_internal = bool(raw.get("isInternal", False))

    return NormalizedComment(
        crm_comment_id=str(crm_id),
        body=body,
        comment_type=raw.get("type", "Post"),
        author_name=author_name,
        author_email=author_email,
        is_internal=is_internal,
        crm_created_at=_parse_dt(raw.get("createdAt")),
        crm_updated_at=_parse_dt(raw.get("modifiedAt")),
        source_system="espocrm",
    )


def normalize_espo_comments(raw_list: list[dict]) -> list[NormalizedComment]:
    """Normalize a list of EspoCRM stream Posts, skipping any that fail."""
    results = []
    for raw in raw_list:
        comment = normalize_espo_comment(raw)
        if comment:
            results.append(comment)
    return results
