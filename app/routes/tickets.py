"""
app/routes/tickets.py  — UPDATED for multitenancy

All routes now inject `get_current_user` (or `require_admin` / `require_agent`).
tenant_id is extracted from the validated JWT and passed to service/repository layer.

EXISTING LOGIC IS UNTOUCHED — only two things change per route:
  1. `current_user: CurrentUser = Depends(get_current_user)` added as param
  2. `tenant_id = current_user.require_tenant()` extracted and passed down

GET  /tickets/                           → paginated list (tenant-scoped)
GET  /tickets/filter                     → filtered list (tenant-scoped)
GET  /tickets/stats                      → dashboard stats (tenant-scoped)
GET  /tickets/stats/agent/{id}           → agent stats (tenant-scoped)
GET  /tickets/by-agent/{id}             → tickets by agent (tenant-scoped)
PUT  /tickets/{id}                       → update ticket (admin only)
GET  /tickets/{id}                       → full detail (tenant-scoped)
GET  /tickets/{id}/comments              → comments (tenant-scoped)
POST /tickets/{id}/comments/sync         → sync comments from CRM
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user, require_admin, require_agent
from app.dependencies import get_db
from app.schemas.agent import AgentBriefResponse
from app.schemas.comment import CommentResponse
from app.schemas.company import CompanyBriefResponse
from app.schemas.customer import CustomerBriefResponse
from app.schemas.ticket import (
    TicketBriefResponse,
    TicketDetailResponse,
    TicketUpdateRequest,
)
from app.services.comment_service import CommentService
from app.services.ticket_service import TicketService
from app.utils.response import paginated, success
from app.schemas.comment import AddCommentRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["Tickets"])


# ---------------------------------------------------------------------------
# Mappers — unchanged from original
# ---------------------------------------------------------------------------


def _to_brief(ticket) -> dict:
    return TicketBriefResponse(
        id=ticket.id,
        title=ticket.title,
        source_system=ticket.source_system.system_name,
        status=ticket.status.status_name,
        priority=ticket.priority.priority_name if ticket.priority else None,
        agent_id=ticket.agent_id,
        customer_id=ticket.customer_id,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        is_deleted=ticket.is_deleted,
    ).model_dump()


def _to_detail(ticket) -> dict:
    return TicketDetailResponse(
        id=ticket.id,
        crm_ticket_id=ticket.crm_ticket_id,
        source_system=ticket.source_system.system_name,
        title=ticket.title,
        description=ticket.description,
        status=ticket.status.status_name,
        priority=ticket.priority.priority_name if ticket.priority else None,
        company=(
            CompanyBriefResponse(
                id=ticket.company.id,
                company_name=ticket.company.company_name,
            )
            if ticket.company
            else None
        ),
        customer=(
            CustomerBriefResponse(
                id=ticket.customer.id,
                # first_name=ticket.customer.first_name,
                # last_name=ticket.customer.last_name,
                name=ticket.customer.name,
                email=ticket.customer.email,
            )
            if ticket.customer
            else None
        ),
        agent=(
            AgentBriefResponse(
                id=ticket.agent.id,
                name=ticket.agent.name,
                email=ticket.agent.email,
            )
            if ticket.agent
            else None
        ),
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        closed_at=ticket.closed_at,
        is_deleted=ticket.is_deleted,
        deleted_at=ticket.deleted_at,
    ).model_dump()


# ---------------------------------------------------------------------------
# GET /tickets/
# ---------------------------------------------------------------------------


@router.get("/", summary="List all tickets for current tenant")
async def list_tickets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_deleted: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    tickets, total = await TicketService(db).get_tickets(
        page=page,
        page_size=page_size,
        include_deleted=include_deleted,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message="Tickets fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/filter
# ---------------------------------------------------------------------------


@router.get("/filter", summary="Filter tickets for current tenant")
async def filter_tickets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    tickets, total = await TicketService(db).filter_tickets(
        page=page,
        page_size=page_size,
        source=source,
        status=status,
        priority=priority,
        include_deleted=include_deleted,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message="Tickets fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/stats
# ---------------------------------------------------------------------------


@router.get("/stats", summary="Dashboard stats for current tenant")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    stats = await TicketService(db).get_stats(tenant_id=uuid.UUID(tenant_id))  # NEW
    return success("Stats fetched successfully", stats)


# ---------------------------------------------------------------------------
# GET /tickets/stats/agent/{agent_id}
# ---------------------------------------------------------------------------


@router.get("/stats/agent/{agent_id}", summary="Stats for a specific agent")
async def get_agent_stats(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    stats = await TicketService(db).get_agent_stats(
        agent_id=agent_id,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return success("Agent stats fetched successfully", stats)


# ---------------------------------------------------------------------------
# GET /tickets/by-agent/{agent_id}
# ---------------------------------------------------------------------------


@router.get("/by-agent/{agent_id}", summary="Tickets assigned to an agent")
async def get_tickets_by_agent(
    agent_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    tickets, total = await TicketService(db).get_tickets_by_agent(
        agent_id=agent_id,
        page=page,
        page_size=page_size,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message="Tickets fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}", summary="Get ticket detail")
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    ticket = await TicketService(db).get_ticket_or_404(
        ticket_id,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return success("Ticket fetched successfully", _to_detail(ticket))


# ---------------------------------------------------------------------------
# PUT /tickets/{ticket_id}  — admin only
# ---------------------------------------------------------------------------


@router.put("/{ticket_id}", summary="Update ticket (admin only)")
async def update_ticket(
    ticket_id: uuid.UUID,
    body: TicketUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),  # NEW (admin gate)
):
    tenant_id = current_user.require_tenant()  # NEW
    ticket = await TicketService(db).update_ticket(
        ticket_id=ticket_id,
        update=body,
        deleted_by_id=None,
        tenant_id=uuid.UUID(tenant_id),  # NEW
    )
    return success("Ticket updated successfully", _to_detail(ticket))


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/comments
# ---------------------------------------------------------------------------


@router.get("/{ticket_id}/comments", summary="List comments for a ticket")
async def list_comments(
    ticket_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()  # NEW
    # Verify ticket belongs to this tenant before returning comments
    await TicketService(db).get_ticket_or_404(ticket_id, tenant_id=uuid.UUID(tenant_id))
    comments, total = await CommentService(db).get_comments_for_ticket(
        ticket_id=ticket_id,
        page=page,
        page_size=page_size,
    )
    return paginated(
        items=[
            CommentResponse(
                id=c.id,
                ticket_id=c.ticket_id,
                body=c.body,
                author_name=c.author_name,
                author_email=c.author_email,
                is_internal=c.is_internal,
                comment_type=c.comment_type,
                crm_created_at=c.crm_created_at,
                # Add these three lines:
                source_system=c.source_system.system_name,  # Access the system_name from the relation
                crm_comment_id=c.crm_comment_id,
                crm_updated_at=c.crm_updated_at,
            ).model_dump()
            for c in comments
        ],
        total=total,
        page=page,
        page_size=page_size,
        message="Comments fetched successfully",
    )


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/comments/sync
# ---------------------------------------------------------------------------


@router.post("/{ticket_id}/comments/sync", summary="Sync comments from CRM")
async def sync_comments(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_agent),  # NEW (agent or admin)
):
    tenant_id = current_user.require_tenant()  # NEW
    await TicketService(db).get_ticket_or_404(ticket_id, tenant_id=uuid.UUID(tenant_id))
    result = await CommentService(db).sync_comments(ticket_id=ticket_id)
    return success("Comments synced", result)
