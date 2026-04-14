"""
app/schemas/ticket.py

Pydantic schemas for the tickets table.

Create / Update use integer IDs (FK values sent by sync service / internal calls).
All Response schemas replace raw FK IDs with human-readable string values:
  - status_id        → status        (e.g. "open")
  - priority_id      → priority      (e.g. "high")  — None if not set
  - source_system_id → source_system (e.g. "zammad")

Update schemas:
  - TicketUpdateRequest : public PUT endpoint schema (role-gated at the route
                          via require_admin — no `role` field needed here)

Pending state contract:
  - When status = "pending", pending_until (datetime) is REQUIRED.
  - When status != "pending", pending_until must be omitted (or null).
  - This is validated at the schema layer so the service never receives
    an inconsistent payload.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.agent import AgentBriefResponse
from app.schemas.company import CompanyBriefResponse
from app.schemas.customer import CustomerBriefResponse

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_VALID_STATUSES   = {"open", "pending", "closed"}
_VALID_PRIORITIES = {"low", "normal", "high", "urgent"}


# ---------------------------------------------------------------------------
# Create — used by sync service only, not exposed as a public API endpoint
# ---------------------------------------------------------------------------

class TicketCreate(BaseModel):
    crm_ticket_id: str = Field(..., max_length=50)
    source_system_id: int
    title: str = Field(..., max_length=255)
    description: str | None = Field(default=None)
    status_id: int
    priority_id: int | None = Field(default=None)
    company_id: UUID | None = Field(default=None)
    customer_id: UUID | None = Field(default=None)
    agent_id: UUID | None = Field(default=None)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = Field(default=None)
    pending_until: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# Agent update — only fields an agent is allowed to change
# ---------------------------------------------------------------------------

class TicketAgentUpdate(BaseModel):
    description: str | None = Field(default=None)
    status_id: int | None = Field(default=None)
    priority_id: int | None = Field(default=None)
    closed_at: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# Admin update — full control over all editable ticket fields
# ---------------------------------------------------------------------------

class TicketAdminUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None)
    status_id: int | None = Field(default=None)
    priority_id: int | None = Field(default=None)
    company_id: UUID | None = Field(default=None)
    customer_id: UUID | None = Field(default=None)
    agent_id: UUID | None = Field(default=None)
    closed_at: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# Public update request — used by PUT /tickets/{id}
#
# Uses human-readable string names (not raw FK IDs).
# Role enforcement is handled at the route level via `require_admin`.
#
# Pending state contract (validated by model_validator below):
#   - status="pending"        → pending_until is REQUIRED
#   - status != "pending"     → pending_until must be None
#   - pending_until without status="pending" → rejected
# ---------------------------------------------------------------------------

class TicketUpdateRequest(BaseModel):
    status: str | None = Field(
        default=None,
        description="Ticket status: open | pending | closed",
    )
    priority: str | None = Field(
        default=None,
        description="Ticket priority: low | normal | high | urgent",
    )
    agent_id: UUID | None = Field(
        default=None,
        description="UUID of the agent to assign this ticket to",
    )
    pending_until: datetime | None = Field(
        default=None,
        description=(
            "Required when status is 'pending'. "
            "The deadline datetime for the pending state (timezone-aware recommended). "
            "Must be omitted or null for all other statuses."
        ),
    )

    @model_validator(mode="after")
    def _validate_pending_contract(self) -> "TicketUpdateRequest":
        """
        Enforce the pending state contract at the schema boundary so that
        the service layer and CRM push logic can trust the payload is consistent.
        """
        status = self.status.lower() if self.status else None

        if status == "pending" and self.pending_until is None:
            raise ValueError(
                "pending_until is required when status is 'pending'. "
                "Provide a future datetime indicating when the pending period ends."
            )

        if status != "pending" and self.pending_until is not None:
            raise ValueError(
                f"pending_until may only be set when status is 'pending', "
                f"got status='{self.status}'."
            )

        return self


# ---------------------------------------------------------------------------
# Soft delete — mirrors the DB CHECK constraint at schema level
# ---------------------------------------------------------------------------

class TicketSoftDelete(BaseModel):
    deleted_by_id: UUID | None = Field(
        default=None,
        description="Dashboard user who deleted — NULL if deleted from CRM side",
    )
    is_deleted_by_crm: bool = Field(
        default=False,
        description="TRUE if CRM deleted it, FALSE if dashboard user deleted it",
    )

    @model_validator(mode="after")
    def validate_deletion_source(self) -> "TicketSoftDelete":
        if self.is_deleted_by_crm is False and self.deleted_by_id is None:
            raise ValueError(
                "deleted_by_id must be set when is_deleted_by_crm is FALSE"
            )
        if self.is_deleted_by_crm is True and self.deleted_by_id is not None:
            raise ValueError(
                "deleted_by_id must be NULL when is_deleted_by_crm is TRUE"
            )
        return self


# ---------------------------------------------------------------------------
# Full flat response
# FK IDs for status, priority, source_system replaced with string names
# ---------------------------------------------------------------------------

class TicketResponse(BaseModel):
    id: UUID
    crm_ticket_id: str
    source_system: str
    title: str
    description: str | None
    status: str
    priority: str | None
    company_id: UUID | None
    customer_id: UUID | None
    agent_id: UUID | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    pending_until: datetime | None
    is_deleted: bool
    deleted_at: datetime | None
    deleted_by_id: UUID | None
    is_deleted_by_crm: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Detail response — used on single ticket GET
# Nested objects for company, customer, agent
# ---------------------------------------------------------------------------

class TicketDetailResponse(BaseModel):
    id: UUID
    crm_ticket_id: str
    source_system: str
    title: str
    description: str | None
    status: str
    priority: str | None
    company: CompanyBriefResponse | None
    customer: CustomerBriefResponse | None
    agent: AgentBriefResponse | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    pending_until: datetime | None
    is_deleted: bool
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Brief response — used in paginated list views
# ---------------------------------------------------------------------------

class TicketBriefResponse(BaseModel):
    id: UUID
    source_system: str
    title: str
    status: str
    priority: str | None
    agent_id: UUID | None
    customer_id: UUID | None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool

    model_config = {"from_attributes": True}