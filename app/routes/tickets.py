"""
app/routes/tickets.py

GET  /tickets/                           → paginated list
GET  /tickets/filter                     → filtered list (?source ?status ?priority ?include_deleted)
GET  /tickets/stats                      → aggregate counts for dashboard
GET  /tickets/stats/agent/{id}           → aggregate counts for a specific agent
GET  /tickets/by-agent/{id}             → tickets assigned to an agent
GET  /tickets/{id}                       → full detail
GET  /tickets/{id}/comments              → paginated comments for a ticket
POST /tickets/{id}/comments/sync         → fetch comments from CRM and store in DB
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.agent import AgentBriefResponse
from app.schemas.comment import CommentResponse
from app.schemas.company import CompanyBriefResponse
from app.schemas.customer import CustomerBriefResponse
from app.schemas.ticket import TicketBriefResponse, TicketDetailResponse
from app.services.comment_service import CommentService
from app.services.ticket_service import TicketService
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["Tickets"])


# ---------------------------------------------------------------------------
# Mappers — ORM object → Pydantic schema dict
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
        company=CompanyBriefResponse(
            id=ticket.company.id,
            company_name=ticket.company.company_name,
        ) if ticket.company else None,
        customer=CustomerBriefResponse(
            id=ticket.customer.id,
            first_name=ticket.customer.first_name,
            last_name=ticket.customer.last_name,
            email=ticket.customer.email,
        ) if ticket.customer else None,
        agent=AgentBriefResponse(
            id=ticket.agent.id,
            name=ticket.agent.name,
            email=ticket.agent.email,
        ) if ticket.agent else None,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        closed_at=ticket.closed_at,
        is_deleted=ticket.is_deleted,
        deleted_at=ticket.deleted_at,
    ).model_dump()


def _to_comment(comment) -> dict:
    return CommentResponse(
        id=comment.id,
        ticket_id=comment.ticket_id,
        source_system=comment.source_system.system_name,
        crm_comment_id=comment.crm_comment_id,
        body=comment.body,
        comment_type=comment.comment_type,
        author_name=comment.author_name,
        author_email=comment.author_email,
        is_internal=comment.is_internal,
        crm_created_at=comment.crm_created_at,
        crm_updated_at=comment.crm_updated_at
    ).model_dump()


# ---------------------------------------------------------------------------
# GET /tickets/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all tickets")
async def list_tickets(
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted tickets"),
    status: str | None = Query(default=None, description="Filter by status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Filter by priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    tickets, total = await TicketService(db).get_tickets(
        page=page,
        page_size=page_size,
        include_deleted=include_deleted,
        status=status,
        priority=priority,
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
# NOTE: defined before /{ticket_id} so "filter" is not parsed as a UUID
# ---------------------------------------------------------------------------

@router.get("/filter", summary="Filter tickets by source, status, or priority")
async def filter_tickets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_deleted: bool = Query(default=False),
    source: str | None = Query(default=None, description="CRM source: zammad, espocrm"),
    status: str | None = Query(default=None, description="Ticket status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Ticket priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    tickets, total = await TicketService(db).filter_tickets(
        page=page,
        page_size=page_size,
        include_deleted=include_deleted,
        source=source,
        status=status,
        priority=priority,
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
# NOTE: defined before /{ticket_id} so "stats" is not parsed as a UUID
# ---------------------------------------------------------------------------

@router.get("/stats", summary="Get ticket stats for dashboard")
async def get_ticket_stats(db: AsyncSession = Depends(get_db)):
    data = await TicketService(db).get_stats()
    return success("Stats fetched successfully", data)


# ---------------------------------------------------------------------------
# GET /tickets/stats/agent/{agent_id}
# ---------------------------------------------------------------------------

@router.get("/stats/agent/{agent_id}", summary="Get ticket stats for a specific agent")
async def get_agent_ticket_stats(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    data = await TicketService(db).get_agent_stats(agent_id)
    return success("Agent stats fetched successfully", data)


# ---------------------------------------------------------------------------
# GET /tickets/by-agent/{agent_id}
# ---------------------------------------------------------------------------

@router.get("/by-agent/{agent_id}", summary="List tickets assigned to a specific agent")
async def list_tickets_by_agent(
    agent_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_deleted: bool = Query(default=False),
    status: str | None = Query(default=None, description="Filter by status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Filter by priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    tickets, total, agent = await TicketService(db).get_tickets_by_agent(
        agent_id=agent_id,
        page=page,
        page_size=page_size,
        include_deleted=include_deleted,
        status=status,
        priority=priority,
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Tickets for agent '{agent.name}' fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}
# ---------------------------------------------------------------------------

@router.get("/{ticket_id}", summary="Get ticket by ID")
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    ticket = await TicketService(db).get_ticket_or_404(ticket_id)
    return success("Ticket fetched successfully", _to_detail(ticket))


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}/comments
# ---------------------------------------------------------------------------

@router.get(
    "/{ticket_id}/comments",
    summary="Get comments for a ticket",
    description=(
        "Returns paginated comments stored in the DB for this ticket. "
        "Comments are populated by calling POST /tickets/{id}/comments/sync first. "
        "Ordered oldest first."
    ),
)
async def get_ticket_comments(
    ticket_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=50, ge=1, le=200, description="Comments per page (max 200)"),
    db: AsyncSession = Depends(get_db),
):
    comments, total = await CommentService(db).get_comments_for_ticket(
        ticket_id=ticket_id,
        page=page,
        page_size=page_size,
    )
    return paginated(
        items=[_to_comment(c) for c in comments],
        total=total,
        page=page,
        page_size=page_size,
        message="Comments fetched successfully",
    )

