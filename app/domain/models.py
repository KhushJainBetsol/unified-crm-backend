# crm/domain/models.py
"""
Unified Domain Models
=====================
These are the canonical, CRM-agnostic data shapes that every adapter must
produce.  Nothing outside the adapter layer ever sees raw Zammad or EspoCRM
JSON — only these models.

Fix (Pydantic v2 OpenAPI crash — TypeError: unhashable type: 'set'):
  PaginatedResult.items was List[Any].  Pydantic v2 cannot deduplicate
  JSON schema definitions when List[Any] appears across multiple routes
  with different concrete item types — it internally creates a set of
  schema dicts which are not hashable.

  Solution: replace List[Any] with List[object].  This is the correct
  Pydantic v2 way to express "a list of anything" in a schema-safe way
  without triggering the deduplication bug.  Do NOT use Generic[T] here —
  Pydantic v2 generic models require all concrete parametrizations to be
  imported at schema-generation time, which causes its own set of issues
  in a dynamic adapter architecture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TicketStatus(str, Enum):
    OPEN     = "open"
    PENDING  = "pending"
    RESOLVED = "resolved"
    CLOSED   = "closed"
    ON_HOLD  = "on_hold"
    UNKNOWN  = "unknown"


class TicketPriority(str, Enum):
    LOW     = "low"
    NORMAL  = "normal"
    HIGH    = "high"
    URGENT  = "urgent"
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
# Shared timestamp validator (avoids repeating the same classmethod 4 times)
# ---------------------------------------------------------------------------

def _parse_dt(v: Any) -> Optional[datetime]:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return _ensure_utc(v) if isinstance(v, datetime) else v


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class UnifiedTicket(BaseModel):
    """A normalised support ticket from any CRM."""

    id:             str
    crm_type:       str
    integration_id: str

    title:       Optional[str]      = None
    description: Optional[str]      = None
    status:      TicketStatus       = TicketStatus.UNKNOWN
    priority:    TicketPriority     = TicketPriority.UNKNOWN
    channel:     Optional[str]      = None
    tags:        List[str]          = Field(default_factory=list)

    assignee_id:     Optional[str] = None
    customer_id:     Optional[str] = None
    organization_id: Optional[str] = None
    group:           Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict, exclude=False)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        return _parse_dt(v)

    model_config = {"populate_by_name": True}


class UnifiedAgent(BaseModel):
    """A normalised CRM agent / user."""

    id:             str
    crm_type:       str
    integration_id: str

    email:      Optional[str] = None
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    active:     bool          = True
    role:       Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        return _parse_dt(v)

    @property
    def full_name(self) -> str:
        parts = filter(None, [self.first_name, self.last_name])
        return " ".join(parts) or self.email or self.id


class UnifiedCustomer(BaseModel):
    """A normalised CRM customer / contact."""

    id:             str
    crm_type:       str
    integration_id: str

    email:      Optional[str] = None
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    active:     bool          = True
    role:       Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        return _parse_dt(v)

    @property
    def full_name(self) -> str:
        parts = filter(None, [self.first_name, self.last_name])
        return " ".join(parts) or self.email or self.id


class UnifiedOrganization(BaseModel):
    """A normalised CRM organization / account."""

    id:             str
    crm_type:       str
    integration_id: str

    name:   Optional[str] = None
    active: bool          = True

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalise_tz(cls, v: Any) -> Optional[datetime]:
        return _parse_dt(v)


# ---------------------------------------------------------------------------
# Pagination envelope
# ---------------------------------------------------------------------------

class PaginatedResult(BaseModel):
    """
    Wraps a page of results with cursor / page metadata.

    FIX: items was List[Any] which caused:
        TypeError: unhashable type: 'set'
    in Pydantic v2's OpenAPI schema generator when the same PaginatedResult
    model appeared across multiple routes with different item types.

    Pydantic v2 tries to build a deduplicated schema definitions dict.
    List[Any] causes it to create a set of schema dicts internally —
    but dicts are not hashable, so it crashes.

    List[object] is the correct Pydantic v2-safe alternative:
    - It produces a stable, hashable JSON schema ("items: {}")
    - It accepts any value at runtime (same as List[Any])
    - It does NOT trigger the deduplication bug
    """

    items:       List[object]
    page:        int           = 1
    per_page:    int           = 100
    total:       Optional[int] = None
    has_more:    bool          = False
    next_cursor: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}

@dataclass
class UnifiedComment:
    """
    CRM-agnostic comment produced by an adapter's fetch_comments().
    Maps 1-to-1 to NormalizedComment before DB upsert.
    """
    id: str                          # CRM-native comment ID
    body: Optional[str]
    comment_type: Optional[str]      # "note", "email", "Post", etc.
    author_name: Optional[str]
    author_email: Optional[str]
    is_internal: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    # first_article signals the Zammad "description" article that
    # should update the ticket body, not be stored as a comment
    is_first_article: bool = field(default=False)