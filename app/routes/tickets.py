"""
app/routes/tickets.py

GET /tickets/         → paginated list   (TicketBriefResponse)
GET /tickets/filter   → filtered list    (TicketBriefResponse)
GET /tickets/{id}     → full detail      (TicketDetailResponse)

Filter query params on /filter:
  ?source=zammad|espocrm
  ?status=open|pending|closed
  ?priority=low|normal|high|urgent
  ?include_deleted=true
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.source_system import SourceSystem
from app.repositories.ticket_repository import TicketRepository
from app.schemas.agent import AgentBriefResponse
from app.schemas.company import CompanyBriefResponse
from app.schemas.customer import CustomerBriefResponse
from app.schemas.ticket import TicketBriefResponse, TicketDetailResponse
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
    offset = (page - 1) * page_size
    tickets, total = await TicketRepository(db).get_all(
        include_deleted=include_deleted,
        status=status,
        priority=priority,
        offset=offset,
        limit=page_size,
    )
    logger.debug("list_tickets: returned %d of %d total", len(tickets), total)
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message="Tickets fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/filter
# IMPORTANT: must be defined BEFORE /{ticket_id} so FastAPI does not
# try to parse the literal string "filter" as a UUID
# ---------------------------------------------------------------------------

@router.get("/filter", summary="Filter tickets by source, status, or priority")
async def filter_tickets(
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted tickets"),
    source: str | None = Query(default=None, description="CRM source: zammad, espocrm"),
    status: str | None = Query(default=None, description="Ticket status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Ticket priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size

    # If source filter provided, resolve it to a source_system_id
    if source:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == source.lower())
        )
        source_obj = result.scalars().first()
        if not source_obj:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Source system '{source}' not found. Valid values: zammad, espocrm",
            )
        tickets, total = await TicketRepository(db).get_by_source_system(
            source_system_id=source_obj.id,
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )
    else:
        tickets, total = await TicketRepository(db).get_all(
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )

    logger.debug("filter_tickets: source=%s status=%s priority=%s returned %d of %d",
                 source, status, priority, len(tickets), total)
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
@router.get("/stats", summary="Get ticket stats for dashboard")
async def get_ticket_stats(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func, case
    from app.models.ticket import Ticket
    from app.models.ticket_status import TicketStatus
    from app.models.ticket_priority import TicketPriority

    # ── Totals ──────────────────────────────────────────────────────────────
    result = await db.execute(
        select(
            func.count(Ticket.id).label("total"),
            func.sum(case((Ticket.is_deleted == False, 1), else_=0)).label("active"),  # noqa: E712
            func.sum(case((Ticket.is_deleted == True,  1), else_=0)).label("deleted"),  # noqa: E712
        )
    )
    row = result.first()

    # ── Count per status (active tickets only) ───────────────────────────────
    status_result = await db.execute(
        select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
        .join(Ticket, Ticket.status_id == TicketStatus.id)
        .where(Ticket.is_deleted == False)  # noqa: E712
        .group_by(TicketStatus.status_name)
    )
    by_status = {r.status_name: r.count for r in status_result}

    # ── Count per priority (active tickets only) ─────────────────────────────
    priority_result = await db.execute(
        select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
        .join(Ticket, Ticket.priority_id == TicketPriority.id)
        .where(Ticket.is_deleted == False)  # noqa: E712
        .group_by(TicketPriority.priority_name)
    )
    by_priority = {r.priority_name: r.count for r in priority_result}

    return success("Stats fetched successfully", {
        # totals
        "total":         row.total   or 0,
        "active":        row.active  or 0,
        "deleted":       row.deleted or 0,

        # widget-ready shortcuts
        "open":          by_status.get("open",    0),
        "closed":        by_status.get("closed",  0),
        "pending":       by_status.get("pending", 0),
        "high_priority": (by_priority.get("high", 0) + by_priority.get("urgent", 0)),

        # full breakdowns for charts
        "by_status":   by_status,
        "by_priority": by_priority,
    })
    
@router.get("/stats/agent/{agent_id}", summary="Get ticket stats for a specific agent")
async def get_agent_ticket_stats(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func, case
    from app.models.ticket import Ticket
    from app.models.ticket_status import TicketStatus
    from app.models.ticket_priority import TicketPriority
    from app.models.agent import Agent

    # Verify agent exists
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalars().first()
    if not agent:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    base = select(func.count(Ticket.id)).where(
        Ticket.agent_id == agent_id,
        Ticket.is_deleted == False,  # noqa: E712
    )

    total_result = await db.execute(base)
    total = total_result.scalar_one()

    # count per status
    status_result = await db.execute(
        select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
        .join(Ticket, Ticket.status_id == TicketStatus.id)
        .where(Ticket.agent_id == agent_id, Ticket.is_deleted == False)  # noqa: E712
        .group_by(TicketStatus.status_name)
    )
    by_status = {r.status_name: r.count for r in status_result}

    # count per priority
    priority_result = await db.execute(
        select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
        .join(Ticket, Ticket.priority_id == TicketPriority.id)
        .where(Ticket.agent_id == agent_id, Ticket.is_deleted == False)  # noqa: E712
        .group_by(TicketPriority.priority_name)
    )
    by_priority = {r.priority_name: r.count for r in priority_result}

    return success("Agent stats fetched successfully", {
        "total":         total,
        "open":          by_status.get("open",    0),
        "closed":        by_status.get("closed",  0),
        "pending":       by_status.get("pending", 0),
        "high_priority": (by_priority.get("high", 0) + by_priority.get("urgent", 0)),
        "by_status":     by_status,
        "by_priority":   by_priority,
    })
    
 # ---------------------------------------------------------------------------
# GET /tickets/by-agent/{agent_id}
# IMPORTANT: must be defined BEFORE /{ticket_id}
# ---------------------------------------------------------------------------

    
@router.get("/by-agent/{agent_id}", summary="List tickets assigned to a specific agent")
async def list_tickets_by_agent(
    agent_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    include_deleted: bool = Query(default=False, description="Include soft-deleted tickets"),
    status: str | None = Query(default=None, description="Filter by status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Filter by priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    from app.models.agent import Agent

    # Verify agent exists
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalars().first()
    if not agent:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    offset = (page - 1) * page_size
    tickets, total = await TicketRepository(db).get_by_agent(
        agent_id=agent_id,
        include_deleted=include_deleted,
        status=status,
        priority=priority,
        offset=offset,
        limit=page_size,
    )
    logger.debug(
        "list_tickets_by_agent: agent=%s status=%s priority=%s returned %d of %d",
        agent_id, status, priority, len(tickets), total,
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Tickets for agent '{agent.name}' fetched successfully",
    )
    

@router.get("/{ticket_id}", summary="Get ticket by ID")
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    ticket = await TicketRepository(db).get_by_id(ticket_id)

    if not ticket:
        logger.warning("get_ticket: ticket %s not found", ticket_id)
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found",
        )

    return success("Ticket fetched successfully", _to_detail(ticket))
