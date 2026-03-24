"""
app/services/ticket_service.py

Business logic for tickets — sits between routes and repositories.

Responsibilities:
  - Source system resolution (name → DB row)
  - Agent existence validation
  - Filter orchestration (which repo method to call)
  - Stats queries
  - get_or_404 helpers

Routes should only call this service and return the response.
All DB-touching logic lives here or in the repository.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.source_system import SourceSystem
from app.models.ticket import Ticket
from app.models.ticket_priority import TicketPriority
from app.models.ticket_status import TicketStatus
from app.repositories.ticket_repository import TicketRepository

logger = logging.getLogger(__name__)


class TicketService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = TicketRepository(db)

    # ------------------------------------------------------------------
    # Source system helpers
    # ------------------------------------------------------------------

    async def _resolve_source_system(self, source: str):
        """
        Resolve a source system name to its DB row.
        Raises HTTP 404 if not found.
        """
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.system_name == source.lower())
        )
        source_obj = result.scalars().first()
        if not source_obj:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Source system '{source}' not found. Valid values: zammad, espocrm",
            )
        return source_obj

    # ------------------------------------------------------------------
    # Agent validation helper
    # ------------------------------------------------------------------

    async def _get_agent_or_404(self, agent_id: uuid.UUID) -> Agent:
        """
        Fetch an agent by UUID or raise HTTP 404.
        """
        result = await self.db.execute(
            select(Agent).where(Agent.id == agent_id)
        )
        agent = result.scalars().first()
        if not agent:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent {agent_id} not found",
            )
        return agent

    # ------------------------------------------------------------------
    # List / filter
    # ------------------------------------------------------------------

    async def get_tickets(
        self,
        page: int,
        page_size: int,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """
        Return paginated list of all tickets, with optional filters.
        """
        offset = (page - 1) * page_size
        return await self.repo.get_all(
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )

    async def filter_tickets(
        self,
        page: int,
        page_size: int,
        include_deleted: bool = False,
        source: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """
        Return paginated tickets with all optional filters applied.
        If source is provided it is resolved to a source_system_id first.
        """
        offset = (page - 1) * page_size

        if source:
            source_obj = await self._resolve_source_system(source)
            return await self.repo.get_by_source_system(
                source_system_id=source_obj.id,
                include_deleted=include_deleted,
                status=status,
                priority=priority,
                offset=offset,
                limit=page_size,
            )

        return await self.repo.get_all(
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )

    async def get_tickets_by_agent(
        self,
        agent_id: uuid.UUID,
        page: int,
        page_size: int,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int, Agent]:
        """
        Validate agent exists, then return their tickets.
        Returns (tickets, total, agent) so the route can use agent.name in the message.
        """
        agent = await self._get_agent_or_404(agent_id)
        offset = (page - 1) * page_size
        tickets, total = await self.repo.get_by_agent(
            agent_id=agent_id,
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )
        return tickets, total, agent

    # ------------------------------------------------------------------
    # Single ticket
    # ------------------------------------------------------------------

    async def get_ticket_or_404(self, ticket_id: uuid.UUID) -> Ticket:
        """
        Fetch a single ticket by UUID or raise HTTP 404.
        """
        ticket = await self.repo.get_by_id(ticket_id)
        if not ticket:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found",
            )
        return ticket

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """
        Aggregate ticket counts: total, active, deleted, by_status, by_priority.
        """
        # Totals
        result = await self.db.execute(
            select(
                func.count(Ticket.id).label("total"),
                func.sum(case((Ticket.is_deleted == False, 1), else_=0)).label("active"),   # noqa: E712
                func.sum(case((Ticket.is_deleted == True,  1), else_=0)).label("deleted"),  # noqa: E712
            )
        )
        row = result.first()

        # Per status (active tickets only)
        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(Ticket.is_deleted == False)  # noqa: E712
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        # Per priority (active tickets only)
        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(Ticket.is_deleted == False)  # noqa: E712
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":         row.total   or 0,
            "active":        row.active  or 0,
            "deleted":       row.deleted or 0,
            "open":          by_status.get("open",    0),
            "closed":        by_status.get("closed",  0),
            "pending":       by_status.get("pending", 0),
            "high_priority": (by_priority.get("high", 0) + by_priority.get("urgent", 0)),
            "by_status":     by_status,
            "by_priority":   by_priority,
        }

    async def get_agent_stats(self, agent_id: uuid.UUID) -> dict:
        """
        Aggregate ticket counts for a specific agent.
        Raises HTTP 404 if agent doesn't exist.
        """
        await self._get_agent_or_404(agent_id)

        total_result = await self.db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.agent_id == agent_id,
                Ticket.is_deleted == False,  # noqa: E712
            )
        )
        total = total_result.scalar_one()

        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(Ticket.agent_id == agent_id, Ticket.is_deleted == False)  # noqa: E712
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(Ticket.agent_id == agent_id, Ticket.is_deleted == False)  # noqa: E712
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":         total,
            "open":          by_status.get("open",    0),
            "closed":        by_status.get("closed",  0),
            "pending":       by_status.get("pending", 0),
            "high_priority": (by_priority.get("high", 0) + by_priority.get("urgent", 0)),
            "by_status":     by_status,
            "by_priority":   by_priority,
        }