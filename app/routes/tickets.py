# """
# app/routes/tickets.py

# GET /tickets/                       → paginated list  (TicketBriefResponse)
# GET /tickets/source/{source_system} → filtered by CRM (TicketBriefResponse)
# GET /tickets/{id}                   → full detail      (TicketDetailResponse)
# """

# from __future__ import annotations

# import logging
# import uuid

# from fastapi import APIRouter, Depends, HTTPException, Query, status
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.dependencies import get_db
# from app.repositories.ticket_repository import TicketRepository
# from app.schemas.agent import AgentBriefResponse
# from app.schemas.company import CompanyBriefResponse
# from app.schemas.customer import CustomerBriefResponse
# from app.schemas.ticket import TicketBriefResponse, TicketDetailResponse
# from app.utils.response import paginated, success

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/tickets", tags=["Tickets"])


# # ---------------------------------------------------------------------------
# # Mappers — ORM object → Pydantic schema
# # ---------------------------------------------------------------------------

# def _to_brief(ticket) -> dict:
#     return TicketBriefResponse(
#         id=ticket.id,
#         title=ticket.title,
#         status=ticket.status.status_name,
#         priority=ticket.priority.priority_name if ticket.priority else None,
#         agent_id=ticket.agent_id,
#         customer_id=ticket.customer_id,
#         created_at=ticket.created_at,
#         updated_at=ticket.updated_at,
#         is_deleted=ticket.is_deleted,
#     ).model_dump()


# def _to_detail(ticket) -> dict:
#     return TicketDetailResponse(
#         id=ticket.id,
#         crm_ticket_id=ticket.crm_ticket_id,
#         source_system=ticket.source_system.system_name,
#         title=ticket.title,
#         description=ticket.description,
#         status=ticket.status.status_name,
#         priority=ticket.priority.priority_name if ticket.priority else None,
#         company=CompanyBriefResponse(
#             id=ticket.company.id,
#             company_name=ticket.company.company_name,
#         ) if ticket.company else None,
#         customer=CustomerBriefResponse(
#             id=ticket.customer.id,
#             first_name=ticket.customer.first_name,
#             last_name=ticket.customer.last_name,
#             email=ticket.customer.email,
#         ) if ticket.customer else None,
#         agent=AgentBriefResponse(
#             id=ticket.agent.id,
#             name=ticket.agent.name,
#             email=ticket.agent.email,
#         ) if ticket.agent else None,
#         created_at=ticket.created_at,
#         updated_at=ticket.updated_at,
#         closed_at=ticket.closed_at,
#         is_deleted=ticket.is_deleted,
#         deleted_at=ticket.deleted_at,
#     ).model_dump()


# # ---------------------------------------------------------------------------
# # GET /tickets/
# # ---------------------------------------------------------------------------

# @router.get("/", summary="List all tickets")
# async def list_tickets(
#     page: int = Query(default=1, ge=1, description="Page number starting from 1"),
#     page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
#     include_deleted: bool = Query(default=False, description="Include soft-deleted tickets"),
#     db: AsyncSession = Depends(get_db),
# ):
#     offset = (page - 1) * page_size
#     tickets, total = await TicketRepository(db).get_all(
#         include_deleted=include_deleted,
#         offset=offset,
#         limit=page_size,
#     )
#     logger.debug("list_tickets: returned %d of %d total", len(tickets), total)
#     return paginated(
#         items=[_to_brief(t) for t in tickets],
#         total=total,
#         page=page,
#         page_size=page_size,
#         message="Tickets fetched successfully",
#     )


# # ---------------------------------------------------------------------------
# # GET /tickets/source/{source_system_name}
# # IMPORTANT: must be defined BEFORE /{ticket_id} so FastAPI does not
# # try to parse the literal string "source" as a UUID
# # ---------------------------------------------------------------------------

# @router.get("/source/{source_system_name}", summary="List tickets by CRM source")
# async def list_tickets_by_source(
#     source_system_name: str,
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
#     include_deleted: bool = Query(default=False),
#     db: AsyncSession = Depends(get_db),
# ):
#     from sqlalchemy import select
#     from app.models.source_system import SourceSystem

#     result = await db.execute(
#         select(SourceSystem).where(
#             SourceSystem.system_name == source_system_name.lower()
#         )
#     )
#     source = result.scalars().first()

#     if not source:
#         logger.warning("list_tickets_by_source: unknown source '%s'", source_system_name)
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"Source system '{source_system_name}' not found. Valid values: zammad, espocrm",
#         )

#     offset = (page - 1) * page_size
#     tickets, total = await TicketRepository(db).get_by_source_system(
#         source_system_id=source.id,
#         include_deleted=include_deleted,
#         offset=offset,
#         limit=page_size,
#     )
#     logger.debug(
#         "list_tickets_by_source: source=%s returned %d of %d",
#         source_system_name, len(tickets), total,
#     )
#     return paginated(
#         items=[_to_brief(t) for t in tickets],
#         total=total,
#         page=page,
#         page_size=page_size,
#         message=f"Tickets for '{source_system_name}' fetched successfully",
#     )


# # ---------------------------------------------------------------------------
# # GET /tickets/{ticket_id}
# # ---------------------------------------------------------------------------

# @router.get("/{ticket_id}", summary="Get ticket by ID")
# async def get_ticket(
#     ticket_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     ticket = await TicketRepository(db).get_by_id(ticket_id)

#     if not ticket:
#         logger.warning("get_ticket: ticket %s not found", ticket_id)
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"Ticket {ticket_id} not found",
#         )

#     return success("Ticket fetched successfully", _to_detail(ticket))


"""
app/routes/tickets.py

GET /tickets/                       → paginated list  (TicketBriefResponse)
GET /tickets/source/{source_system} → filtered by CRM (TicketBriefResponse)
GET /tickets/{id}                   → full detail      (TicketDetailResponse)

Filters available on list endpoints:
  ?status=open|pending|closed
  ?priority=low|normal|high|urgent
  ?include_deleted=true
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
        source_system=ticket.source_system.system_name,
        title=ticket.title,
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
# GET /tickets/source/{source_system_name}
# IMPORTANT: must be defined BEFORE /{ticket_id} so FastAPI does not
# try to parse the literal string "source" as a UUID
# ---------------------------------------------------------------------------

@router.get("/filters", summary="List tickets by filters")
async def list_tickets_by_source(
    source_system_name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_deleted: bool = Query(default=False),
    status: str | None = Query(default=None, description="Filter by status: open, pending, closed"),
    priority: str | None = Query(default=None, description="Filter by priority: low, normal, high, urgent"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SourceSystem).where(
            SourceSystem.system_name == source_system_name.lower()
        )
    )
    source = result.scalars().first()

    if not source:
        logger.warning("list_tickets_by_source: unknown source '%s'", source_system_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source system '{source_system_name}' not found. Valid values: zammad, espocrm",
        )

    offset = (page - 1) * page_size
    tickets, total = await TicketRepository(db).get_by_source_system(
        source_system_id=source.id,
        include_deleted=include_deleted,
        status=status,
        priority=priority,
        offset=offset,
        limit=page_size,
    )
    logger.debug(
        "list_tickets_by_source: source=%s returned %d of %d",
        source_system_name, len(tickets), total,
    )
    return paginated(
        items=[_to_brief(t) for t in tickets],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Tickets for '{source_system_name}' fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /tickets/{ticket_id}
# ---------------------------------------------------------------------------

@router.get("/{ticket_id}", summary="Get ticket by ID")
async def get_ticket(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    ticket = await TicketRepository(db).get_by_id(ticket_id)

    if not ticket:
        logger.warning("get_ticket: ticket %s not found", ticket_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket {ticket_id} not found",
        )

    return success("Ticket fetched successfully", _to_detail(ticket))