"""
app/integrations/normalizer/schema.py

NormalizedTicket — the single internal contract that every CRM normalizer
must produce regardless of source system.

This is NOT a Pydantic API schema.
It is a plain dataclass used only within the integration and service layers.
The sync service converts this into TicketCreate (Pydantic) before hitting
the repository.

Field naming follows YOUR database column conventions, not any CRM's.
All IDs are strings because Zammad uses integers and EspoCRM uses UUID strings —
casting everything to str here keeps downstream code CRM-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NormalizedTicket:
    # ----------------------------------------------------------------
    # CRM origin
    # ----------------------------------------------------------------
    crm_ticket_id: str
    source_system: str          # "zammad" | "espocrm"

    # ----------------------------------------------------------------
    # Core fields
    # ----------------------------------------------------------------
    title: str
    description: str | None

    # ----------------------------------------------------------------
    # Lookup values — YOUR standard strings, not the CRM's raw values
    # status   : "open" | "pending" | "closed"
    # priority : "low"  | "normal"  | "high" | "urgent" | None
    # ----------------------------------------------------------------
    status: str
    priority: str | None

    # ----------------------------------------------------------------
    # Related entity IDs — original CRM IDs (always str)
    # Sync service uses these to look up internal UUIDs
    # ----------------------------------------------------------------
    crm_agent_id: str | None
    crm_customer_id: str | None
    crm_company_id: str | None

    # ----------------------------------------------------------------
    # Timestamps
    # ----------------------------------------------------------------
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = field(default=None)

    def is_closed(self) -> bool:
        return self.status == "closed"

    def __repr__(self) -> str:
        return (
            f"<NormalizedTicket crm_id={self.crm_ticket_id!r} "
            f"source={self.source_system!r} status={self.status!r}>"
        )
