"""
app/services/ticket_service.py

Business logic for tickets — sits between routes and repositories.

Responsibilities:
  - Source system resolution (name → DB row)
  - Agent existence validation
  - Filter orchestration (which repo method to call)
  - Stats queries (tenant-scoped)
  - get_or_404 helpers (tenant-scoped)
  - Role-gated ticket updates with CRM push

Multitenancy:
  - Every public method accepts tenant_id: uuid.UUID | None.
  - It is passed down to the repository and to every raw SQL query in
    this file (stats). Never skip it in a multitenant request.

Routes should only call this service and return the response.
All DB-touching logic lives here or in the repository.

CRM push strategy:
  - DB is always updated first and committed.
  - CRM push is best-effort: failures are logged but never raised to the caller.
  - This means a CRM outage never rolls back a user's dashboard action.
  - If CRM push fails, investigate and re-sync manually.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# NEW: Import the factory instead of specific CRM clients
from app.factory.adapter_factory import CrmAdapterFactory

from app.models.agent import Agent
from app.models.source_system import SourceSystem
from app.models.ticket import Ticket
from app.models.ticket_priority import TicketPriority
from app.models.ticket_status import TicketStatus
from app.models.crm_integration import CrmIntegration  # Needed to look up integration_id
from app.repositories.ticket_repository import TicketRepository
from app.schemas.ticket import TicketUpdateRequest

logger = logging.getLogger(__name__)


class TicketService:
    def __init__(
        self, 
        db: AsyncSession, 
        adapter_factory: CrmAdapterFactory  # INJECT THE FACTORY HERE
    ) -> None:
        self.db = db
        self.repo = TicketRepository(db)
        self.factory = adapter_factory

    # ------------------------------------------------------------------
    # Source system helpers
    # ------------------------------------------------------------------

    async def _resolve_source_system(self, source: str) -> SourceSystem:
        """Resolve a source system name to its DB row. Raises HTTP 404 if not found."""
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
        """Fetch an agent by UUID or raise HTTP 404."""
        result = await self.db.execute(select(Agent).where(Agent.id == agent_id))
        agent  = result.scalars().first()
        if not agent:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent {agent_id} not found",
            )
        return agent

    # ------------------------------------------------------------------
    # Status / priority lookup helpers
    # ------------------------------------------------------------------

    async def _resolve_status(self, status_name: str) -> TicketStatus:
        """Resolve a status string to its DB row. Raises HTTP 422 if invalid."""
        result = await self.db.execute(
            select(TicketStatus).where(TicketStatus.status_name == status_name.lower())
        )
        obj = result.scalars().first()
        if not obj:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid status '{status_name}'. Valid values: open, pending, closed",
            )
        return obj

    async def _resolve_priority(self, priority_name: str) -> TicketPriority:
        """Resolve a priority string to its DB row. Raises HTTP 422 if invalid."""
        result = await self.db.execute(
            select(TicketPriority).where(TicketPriority.priority_name == priority_name.lower())
        )
        obj = result.scalars().first()
        if not obj:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid priority '{priority_name}'. Valid values: low, normal, high, urgent",
            )
        return obj

    # ------------------------------------------------------------------
    # List / filter
    # ------------------------------------------------------------------

    async def get_tickets(
        self,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """Return paginated list of all tickets for a tenant."""
        offset = (page - 1) * page_size
        return await self.repo.get_all(
            tenant_id=tenant_id,
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
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        source: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """Return paginated tickets with all optional filters applied."""
        offset = (page - 1) * page_size

        if source:
            source_obj = await self._resolve_source_system(source)
            return await self.repo.get_by_source_system(
                source_system_id=source_obj.id,
                tenant_id=tenant_id,
                include_deleted=include_deleted,
                status=status,
                priority=priority,
                offset=offset,
                limit=page_size,
            )

        return await self.repo.get_all(
            tenant_id=tenant_id,
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
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int, Agent]:
        """Validate agent exists, then return their tickets scoped to tenant."""
        agent  = await self._get_agent_or_404(agent_id)
        offset = (page - 1) * page_size
        tickets, total = await self.repo.get_by_agent(
            agent_id=agent_id,
            tenant_id=tenant_id,
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

    async def get_ticket_or_404(
        self,
        ticket_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket:
        """Fetch a single ticket by UUID, scoped to tenant, or raise HTTP 404."""
        ticket = await self.repo.get_by_id(ticket_id, tenant_id=tenant_id)
        if not ticket:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found",
            )
        return ticket

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_ticket(
        self,
        ticket_id: uuid.UUID,
        update: TicketUpdateRequest,
        deleted_by_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket:
        """
        Apply a partial update to a ticket, then push the change to the CRM.
        """
        ticket = await self.get_ticket_or_404(ticket_id, tenant_id=tenant_id)

        update_data: dict = {}

        if update.status is not None:
            status_obj = await self._resolve_status(update.status)
            update_data["status_id"] = status_obj.id
            new_status = update.status.lower()

            # --- closed_at lifecycle ---
            if new_status == "closed" and ticket.closed_at is None:
                update_data["closed_at"] = datetime.utcnow()
            elif new_status != "closed" and ticket.closed_at is not None:
                ticket.closed_at = None

            # --- pending_until lifecycle ---
            if new_status == "pending":
                update_data["pending_until"] = update.pending_until
            elif ticket.pending_until is not None:
                ticket.pending_until = None

        if update.priority is not None:
            priority_obj = await self._resolve_priority(update.priority)
            update_data["priority_id"] = priority_obj.id

        if update.agent_id is not None:
            await self._get_agent_or_404(update.agent_id)
            update_data["agent_id"] = update.agent_id

        if not update_data:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Request body contained no updatable fields",
            )

        updated_ticket = await self.repo.update(ticket, update_data)

        # Best-effort CRM push — never raises to the caller.
        await self._push_update_to_crm(updated_ticket, update)

        return updated_ticket

    # ------------------------------------------------------------------
    # CRM push — dispatcher (MASSIVELY SIMPLIFIED)
    # ------------------------------------------------------------------

    async def _push_update_to_crm(
        self,
        ticket: Ticket,
        payload: TicketUpdateRequest,
    ) -> None:
        try:
            result = await self.db.execute(
                select(CrmIntegration).where(
                    CrmIntegration.tenant_id == ticket.tenant_id,
                    CrmIntegration.source_system_id == ticket.source_system_id,
                    CrmIntegration.is_active == True,
                )
            )
            integration = result.scalars().first()

            if not integration:
                logger.warning(
                    "No active CRM integration for tenant=%s system=%s — skipping push.",
                    ticket.tenant_id,
                    ticket.source_system_id,
                )
                return

            # Pass the ORM object so the factory doesn't need a second DB call
            adapter = self.factory.create(
                str(integration.id),
                integration_obj=integration,   # ← the only change
            )

            async with adapter:
                await adapter.push_ticket_update(ticket.crm_ticket_id, payload)

        except Exception as exc:
            logger.error(
                "CRM push failed for ticket %s: %s — DB already updated.",
                ticket.id,
                exc,
            )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self, tenant_id: uuid.UUID | None = None) -> dict:
        """Aggregate ticket counts for a tenant: total, active, deleted, by_status, by_priority."""
        base_filter = [Ticket.tenant_id == tenant_id] if tenant_id is not None else []

        result = await self.db.execute(
            select(
                func.count(Ticket.id).label("total"),
                func.sum(case((Ticket.is_deleted == False, 1), else_=0)).label("active"),  # noqa: E712
                func.sum(case((Ticket.is_deleted == True, 1), else_=0)).label("deleted"),   # noqa: E712
            ).where(*base_filter)
        )
        row = result.first()

        active_filter = [*base_filter, Ticket.is_deleted == False]  # noqa: E712

        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(*active_filter)
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(*active_filter)
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":          row.total or 0,
            "active":         row.active or 0,
            "deleted":        row.deleted or 0,
            "open":           by_status.get("open", 0),
            "closed":         by_status.get("closed", 0),
            "pending":        by_status.get("pending", 0),
            "high_priority":  by_priority.get("high", 0) + by_priority.get("urgent", 0),
            "by_status":      by_status,
            "by_priority":    by_priority,
        }

    async def get_agent_stats(
        self,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> dict:
        """Aggregate ticket counts for a specific agent, scoped to tenant."""
        await self._get_agent_or_404(agent_id)

        base_filter = [
            Ticket.agent_id  == agent_id,
            Ticket.is_deleted == False,  # noqa: E712
        ]
        if tenant_id is not None:
            base_filter.append(Ticket.tenant_id == tenant_id)

        total_result = await self.db.execute(
            select(func.count(Ticket.id)).where(*base_filter)
        )
        total = total_result.scalar_one()

        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(*base_filter)
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(*base_filter)
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":         total,
            "open":          by_status.get("open", 0),
            "closed":        by_status.get("closed", 0),
            "pending":       by_status.get("pending", 0),
            "high_priority": by_priority.get("high", 0) + by_priority.get("urgent", 0),
            "by_status":     by_status,
            "by_priority":   by_priority,
        }