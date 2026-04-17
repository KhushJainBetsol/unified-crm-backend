# crm/domain/models.py
"""
Unified Domain Models
=====================
These are the canonical, CRM-agnostic data shapes that every adapter must
produce.  Nothing outside the adapter layer ever sees raw Zammad or EspoCRM
JSON — only these models.

Design rules:
- All fields are Optional where the source CRM may not carry the data.
- datetime fields are always UTC-aware (enforced by validator).
- Enums use plain strings with a small validated set so serialisation is
  trivially JSON-compatible.
- Each model carries a `raw` dict for debugging / passthrough use-cases
  without polluting the typed surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TicketStatus(str, Enum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ON_HOLD = "on_hold"
    UNKNOWN = "unknown"


class TicketPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_utc(v: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware (UTC). Naive → UTC assumed."""
    if v is None:
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class UnifiedTicket(BaseModel):
    """A normalised support ticket from any CRM."""

    # --- Identity ---
    id: str
    crm_type: str                        # e.g. "zammad", "espocrm"
    integration_id: str                  # tenant reference

    # --- Core fields ---
    title: Optional[str] = None
    description: Optional[str] = None
    status: TicketStatus = TicketStatus.UNKNOWN
    priority: TicketPriority = TicketPriority.UNKNOWN
    channel: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    # --- Relations ---
    assignee_id: Optional[str] = None
    customer_id: Optional[str] = None
    organization_id: Optional[str] = None
    group: Optional[str] = None

    # --- Timestamps (always UTC) ---
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # --- Passthrough ---
    raw: Dict[str, Any] = Field(default_factory=dict, exclude=False)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return _ensure_utc(v) if isinstance(v, datetime) else v

    model_config = {"populate_by_name": True}


class UnifiedAgent(BaseModel):
    """A normalised CRM agent / user."""

    id: str
    crm_type: str
    integration_id: str

    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    active: bool = True
    role: Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return _ensure_utc(v) if isinstance(v, datetime) else v

    @property
    def full_name(self) -> str:
        parts = filter(None, [self.first_name, self.last_name])
        return " ".join(parts) or self.email or self.id


class UnifiedOrganization(BaseModel):
    """A normalised CRM organization / account."""

    id: str
    crm_type: str
    integration_id: str

    name: Optional[str] = None
    active: bool = True

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return _ensure_utc(v) if isinstance(v, datetime) else v


# ---------------------------------------------------------------------------
# Pagination envelope — returned by paginated fetch methods
# ---------------------------------------------------------------------------

class PaginatedResult(BaseModel):
    """Wraps a page of results with cursor / page metadata."""

    items: List[Any]
    page: int = 1
    per_page: int = 100
    total: Optional[int] = None        # None when CRM doesn't provide a total
    has_more: bool = False
    next_cursor: Optional[str] = None  # for cursor-based pagination