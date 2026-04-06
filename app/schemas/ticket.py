# """
# app/schemas/ticket.py

# Pydantic schemas for the tickets table.

# Create / Update use integer IDs (FK values sent by sync service / internal calls).
# All Response schemas replace raw FK IDs with human-readable string values:
#   - status_id        → status        (e.g. "open")
#   - priority_id      → priority      (e.g. "high")  — None if not set
#   - source_system_id → source_system (e.g. "zammad")

# Update schemas:
#   - TicketUpdateRequest : public PUT endpoint schema (role-gated in the service)

# Response shapes:
#   - TicketResponse       : full flat response
#   - TicketDetailResponse : nested company / customer / agent objects
#   - TicketBriefResponse  : lightweight for paginated list views
# """

# from __future__ import annotations

# from datetime import datetime
# from uuid import UUID

# from pydantic import BaseModel, Field, model_validator

# from app.schemas.agent import AgentBriefResponse
# from app.schemas.company import CompanyBriefResponse
# from app.schemas.customer import CustomerBriefResponse


# # ---------------------------------------------------------------------------
# # Create — used by sync service only, not exposed as a public API endpoint
# # ---------------------------------------------------------------------------
# class TicketCreate(BaseModel):
#     crm_ticket_id: str = Field(..., max_length=50)
#     source_system_id: int
#     title: str = Field(..., max_length=255)
#     description: str | None = Field(default=None)
#     status_id: int
#     priority_id: int | None = Field(default=None)
#     company_id: UUID | None = Field(default=None)
#     customer_id: UUID | None = Field(default=None)
#     agent_id: UUID | None = Field(default=None)
#     created_at: datetime
#     updated_at: datetime
#     closed_at: datetime | None = Field(default=None)


# # ---------------------------------------------------------------------------
# # Agent update — only fields an agent is allowed to change
# # Cannot reassign company, customer, agent or change the title
# # ---------------------------------------------------------------------------
# class TicketAgentUpdate(BaseModel):
#     description: str | None = Field(default=None)
#     status_id: int | None = Field(default=None)
#     priority_id: int | None = Field(default=None)
#     closed_at: datetime | None = Field(default=None)


# # ---------------------------------------------------------------------------
# # Admin update — full control over all editable ticket fields
# # ---------------------------------------------------------------------------
# class TicketAdminUpdate(BaseModel):
#     title: str | None = Field(default=None, max_length=255)
#     description: str | None = Field(default=None)
#     status_id: int | None = Field(default=None)
#     priority_id: int | None = Field(default=None)
#     company_id: UUID | None = Field(default=None)
#     customer_id: UUID | None = Field(default=None)
#     agent_id: UUID | None = Field(default=None)
#     closed_at: datetime | None = Field(default=None)


# # ---------------------------------------------------------------------------
# # Public update request — used by PUT /tickets/{id}
# #
# # Uses human-readable string names (not raw FK IDs).
# # Role enforcement is handled in TicketService.update_ticket(), not here.
# #
# # `role` is passed directly from the frontend until Keycloak is integrated.
# # Once auth is live:
# #   - Remove the `role` field from this schema
# #   - Pass role from the decoded JWT in the route instead
# # ---------------------------------------------------------------------------
# class TicketUpdateRequest(BaseModel):
#     role: str = Field(
#         ...,
#         description=(
#             "Caller role: 'agent' | 'admin'  "
#             "— temporary until Keycloak auth is integrated"
#         ),
#     )
#     status: str | None = Field(
#         default=None,
#         description="Ticket status: open | pending | closed",
#     )
#     priority: str | None = Field(
#         default=None,
#         description="Ticket priority: low | normal | high | urgent  (admin only)",
#     )
#     agent_id: UUID | None = Field(
#         default=None,
#         description="UUID of the agent to assign this ticket to  (admin only)",
#     )


# # ---------------------------------------------------------------------------
# # Soft delete — mirrors the DB CHECK constraint at schema level
# # ---------------------------------------------------------------------------
# class TicketSoftDelete(BaseModel):
#     deleted_by_id: UUID | None = Field(
#         default=None,
#         description="Dashboard user who deleted — NULL if deleted from CRM side",
#     )
#     deleted_by_source: bool = Field(
#         default=False,
#         description="TRUE if CRM deleted it, FALSE if dashboard user deleted it",
#     )

#     @model_validator(mode="after")
#     def validate_deletion_source(self) -> "TicketSoftDelete":
#         if self.deleted_by_source is False and self.deleted_by_id is None:
#             raise ValueError(
#                 "deleted_by_id must be set when deleted_by_source is FALSE"
#             )
#         if self.deleted_by_source is True and self.deleted_by_id is not None:
#             raise ValueError(
#                 "deleted_by_id must be NULL when deleted_by_source is TRUE"
#             )
#         return self


# # ---------------------------------------------------------------------------
# # Full flat response
# # FK IDs for status, priority, source_system replaced with string names
# # ---------------------------------------------------------------------------
# class TicketResponse(BaseModel):
#     id: UUID
#     crm_ticket_id: str
#     source_system: str
#     title: str
#     description: str | None
#     status: str
#     priority: str | None
#     company_id: UUID | None
#     customer_id: UUID | None
#     agent_id: UUID | None
#     created_at: datetime
#     updated_at: datetime
#     closed_at: datetime | None
#     is_deleted: bool
#     deleted_at: datetime | None
#     deleted_by_id: UUID | None
#     deleted_by_source: bool

#     model_config = {"from_attributes": True}


# # ---------------------------------------------------------------------------
# # Detail response — used on single ticket GET
# # Nested objects for company, customer, agent
# # ---------------------------------------------------------------------------
# class TicketDetailResponse(BaseModel):
#     id: UUID
#     crm_ticket_id: str
#     source_system: str
#     title: str
#     description: str | None
#     status: str
#     priority: str | None
#     company: CompanyBriefResponse | None
#     customer: CustomerBriefResponse | None
#     agent: AgentBriefResponse | None
#     created_at: datetime
#     updated_at: datetime
#     closed_at: datetime | None
#     is_deleted: bool
#     deleted_at: datetime | None

#     model_config = {"from_attributes": True}


# # ---------------------------------------------------------------------------
# # Brief response — used in paginated list views
# # ---------------------------------------------------------------------------
# class TicketBriefResponse(BaseModel):
#     id: UUID
#     source_system: str
#     title: str
#     status: str
#     priority: str | None
#     agent_id: UUID | None
#     customer_id: UUID | None
#     created_at: datetime
#     updated_at: datetime
#     is_deleted: bool

#     model_config = {"from_attributes": True}

"""
app/schemas/ticket.py

Pydantic schemas for the tickets table.

Create / Update use integer IDs (FK values sent by sync service / internal calls).
All Response schemas replace raw FK IDs with human-readable string values:
  - status_id        → status        (e.g. "open")
  - priority_id      → priority      (e.g. "high")  — None if not set
  - source_system_id → source_system (e.g. "zammad")

Update schemas:
  - TicketUpdateRequest : public PUT endpoint schema (role-gated in the service)

Response shapes:
  - TicketResponse       : full flat response
  - TicketDetailResponse : nested company / customer / agent objects
  - TicketBriefResponse  : lightweight for paginated list views
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator, EmailStr

from app.schemas.agent import AgentBriefResponse
from app.schemas.company import CompanyBriefResponse
from app.schemas.customer import CustomerBriefResponse


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


# ---------------------------------------------------------------------------
# Agent update — only fields an agent is allowed to change
# Cannot reassign company, customer, agent or change the title
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
# Role enforcement is handled in TicketService.update_ticket(), not here.
#
# `role` is passed directly from the frontend until Keycloak is integrated.
# Once auth is live:
#   - Remove the `role` field from this schema
#   - Pass role from the decoded JWT in the route instead
# ---------------------------------------------------------------------------
class TicketUpdateRequest(BaseModel):
    role: str = Field(
        ...,
        description=(
            "Caller role: 'agent' | 'admin'  "
            "— temporary until Keycloak auth is integrated"
        ),
    )
    status: str | None = Field(
        default=None,
        description="Ticket status: open | pending | closed",
    )
    priority: str | None = Field(
        default=None,
        description="Ticket priority: low | normal | high | urgent  (admin only)",
    )
    agent_id: UUID | None = Field(
        default=None,
        description="UUID of the agent to assign this ticket to  (admin only)",
    )


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
