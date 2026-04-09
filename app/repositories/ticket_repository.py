"""
app/repositories/ticket_repository.py

Database queries for the tickets table — no business logic here.

Every query that returns a response with human-readable strings
uses joinedload to fetch related lookup rows in the same SQL query
avoiding N+1 problems.

Loaded relationships per query:
  - Ticket.status        → ticket_status.status_name
  - Ticket.priority      → ticket_priority.priority_name
  - Ticket.source_system → source_systems.system_name
  - Ticket.company       → companies (brief)
  - Ticket.customer      → customers (brief)
  - Ticket.agent         → agents (brief)

Multitenancy:
  - Every query accepts an optional tenant_id: uuid.UUID | None.
  - When provided it is always added as a WHERE clause — this is the
    primary data-isolation guard. Never call these methods without
    passing tenant_id in a multitenant context.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.ticket import Ticket
from app.models.ticket_priority import TicketPriority
from app.models.ticket_status import TicketStatus


def _base_query():
    """
    Base SELECT with all joinedloads applied.
    Every read query builds on top of this so joins are never forgotten.
    """
    return (
        select(Ticket)
        .options(
            joinedload(Ticket.status),
            joinedload(Ticket.priority),
            joinedload(Ticket.source_system),
            joinedload(Ticket.company),
            joinedload(Ticket.customer),
            joinedload(Ticket.agent),
        )
    )


def _apply_tenant(query, count_query, tenant_id: uuid.UUID | None):
    """
    Apply tenant_id filter to both the main query and its count twin.
    Always call this immediately after constructing the base queries.
    """
    if tenant_id is not None:
        query = query.where(Ticket.tenant_id == tenant_id)
        count_query = count_query.where(Ticket.tenant_id == tenant_id)
    return query, count_query


def _apply_filters(
    query,
    count_query,
    include_deleted: bool,
    status: str | None,
    priority: str | None,
):
    """
    Apply soft-delete + status/priority filters to both query twins.
    Extracted so every list method stays DRY.
    """
    if not include_deleted:
        query = query.where(Ticket.is_deleted == False)           # noqa: E712
        count_query = count_query.where(Ticket.is_deleted == False)  # noqa: E712

    if status:
        query = query.join(
            TicketStatus, Ticket.status_id == TicketStatus.id
        ).where(TicketStatus.status_name == status.lower())
        count_query = count_query.join(
            TicketStatus, Ticket.status_id == TicketStatus.id
        ).where(TicketStatus.status_name == status.lower())

    if priority:
        query = query.join(
            TicketPriority, Ticket.priority_id == TicketPriority.id
        ).where(TicketPriority.priority_name == priority.lower())
        count_query = count_query.join(
            TicketPriority, Ticket.priority_id == TicketPriority.id
        ).where(TicketPriority.priority_name == priority.lower())

    return query, count_query


class TicketRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list
    # ------------------------------------------------------------------

    async def get_all(
        self,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """
        Fetch a paginated list of tickets with total count.

        Args:
            tenant_id:       Scope results to this tenant. Always pass this.
            include_deleted: If False (default) excludes soft-deleted tickets.
            status:          Filter by status name, e.g. "open", "closed".
            priority:        Filter by priority name, e.g. "high", "urgent".
            offset:          Number of records to skip.
            limit:           Max records to return.

        Returns:
            Tuple of (list of Ticket ORM objects, total count).
        """
        query = _base_query()
        count_query = select(func.count()).select_from(Ticket)

        query, count_query = _apply_tenant(query, count_query, tenant_id)
        query, count_query = _apply_filters(
            query, count_query, include_deleted, status, priority
        )

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Ticket.created_at.desc())
        result = await self.db.execute(query)
        tickets = list(result.scalars().unique().all())

        return tickets, total

    async def get_by_source_system(
        self,
        source_system_id: int,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """
        Fetch tickets belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            tenant_id:        Scope results to this tenant. Always pass this.
            include_deleted:  If False excludes soft-deleted tickets.
            status:           Filter by status name, e.g. "open", "closed".
            priority:         Filter by priority name, e.g. "high", "urgent".
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Ticket ORM objects, total count).
        """
        query = _base_query().where(Ticket.source_system_id == source_system_id)
        count_query = (
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.source_system_id == source_system_id)
        )

        query, count_query = _apply_tenant(query, count_query, tenant_id)
        query, count_query = _apply_filters(
            query, count_query, include_deleted, status, priority
        )

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Ticket.created_at.desc())
        result = await self.db.execute(query)
        tickets = list(result.scalars().unique().all())

        return tickets, total

    async def get_by_agent(
        self,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """
        Fetch tickets assigned to a specific agent.

        Args:
            agent_id:        Internal UUID of the agent.
            tenant_id:       Scope results to this tenant. Always pass this.
            include_deleted: If False excludes soft-deleted tickets.
            status:          Filter by status name, e.g. "open", "closed".
            priority:        Filter by priority name, e.g. "high", "urgent".
            offset:          Number of records to skip.
            limit:           Max records to return.

        Returns:
            Tuple of (list of Ticket ORM objects, total count).
        """
        query = _base_query().where(Ticket.agent_id == agent_id)
        count_query = (
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.agent_id == agent_id)
        )

        query, count_query = _apply_tenant(query, count_query, tenant_id)
        query, count_query = _apply_filters(
            query, count_query, include_deleted, status, priority
        )

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Ticket.created_at.desc())
        result = await self.db.execute(query)
        tickets = list(result.scalars().unique().all())

        return tickets, total

    # ------------------------------------------------------------------
    # READ — single
    # ------------------------------------------------------------------

    async def get_by_id(
        self,
        ticket_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket | None:
        """
        Fetch a single ticket by internal UUID, scoped to tenant.

        Args:
            ticket_id: Internal UUID of the ticket.
            tenant_id: Scope to this tenant. Always pass this.

        Returns:
            Ticket ORM object or None if not found (or belongs to another tenant).
        """
        query = _base_query().where(Ticket.id == ticket_id)
        if tenant_id is not None:
            query = query.where(Ticket.tenant_id == tenant_id)
        result = await self.db.execute(query)
        return result.scalars().first()

    async def get_by_crm_id(
        self,
        crm_ticket_id: str,
        source_system_id: int,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket | None:
        """
        Fetch a ticket by its original CRM ID + source system.
        Used by the sync service to check if a ticket already exists
        before deciding to insert or update.

        Args:
            crm_ticket_id:    Original ticket ID from the CRM.
            source_system_id: FK id of the source system.
            tenant_id:        Scope to this tenant. Always pass this.

        Returns:
            Ticket ORM object or None.
        """
        query = _base_query().where(
            Ticket.crm_ticket_id == crm_ticket_id,
            Ticket.source_system_id == source_system_id,
        )
        if tenant_id is not None:
            query = query.where(Ticket.tenant_id == tenant_id)
        result = await self.db.execute(query)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # WRITE — create
    # ------------------------------------------------------------------

    async def create(self, data: dict) -> Ticket:
        """
        Insert a new ticket row.

        Args:
            data: Dict of column values matching the Ticket model.
                  Must include all required fields (including tenant_id).

        Returns:
            The newly created Ticket ORM object (with id populated).
        """
        ticket = Ticket(**data)
        self.db.add(ticket)
        await self.db.flush()       # flush to get the generated id
        await self.db.refresh(ticket)
        return ticket

    # ------------------------------------------------------------------
    # WRITE — update
    # ------------------------------------------------------------------

    async def update(
        self,
        ticket: Ticket,
        data: dict,
    ) -> Ticket:
        """
        Update an existing ticket with the provided field values.
        Only updates fields present in `data` — skips None values.

        Args:
            ticket: Existing Ticket ORM object fetched from DB.
            data:   Dict of fields to update (None values are skipped).

        Returns:
            Updated Ticket ORM object.
        """
        for field, value in data.items():
            if value is not None:
                setattr(ticket, field, value)

        await self.db.flush()
        await self.db.refresh(ticket)
        return ticket

    # ------------------------------------------------------------------
    # WRITE — upsert (used by sync service)
    # ------------------------------------------------------------------

    async def upsert(
        self,
        crm_ticket_id: str,
        source_system_id: int,
        data: dict,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[Ticket, bool]:
        """
        Insert if not exists, update if exists.
        The sync service calls this for every ticket from the CRM.

        Args:
            crm_ticket_id:    Original CRM ticket ID.
            source_system_id: FK id of the source system.
            data:             Full dict of ticket field values.
            tenant_id:        Scope the existence check to this tenant.

        Returns:
            Tuple of (Ticket ORM object, created: bool).
            created=True means it was inserted, False means updated.
        """
        existing = await self.get_by_crm_id(
            crm_ticket_id, source_system_id, tenant_id=tenant_id
        )

        if existing:
            updated = await self.update(existing, data)
            return updated, False
        else:
            data["crm_ticket_id"] = crm_ticket_id
            data["source_system_id"] = source_system_id
            if tenant_id is not None:
                data.setdefault("tenant_id", tenant_id)
            created = await self.create(data)
            return created, True

    # ------------------------------------------------------------------
    # WRITE — soft delete
    # ------------------------------------------------------------------

    async def soft_delete(
        self,
        ticket: Ticket,
        deleted_by_id: uuid.UUID | None,
        is_deleted_by_crm: bool,
    ) -> Ticket:
        """
        Soft-delete a ticket by setting is_deleted + deleted_at.

        Args:
            ticket:            Ticket ORM object to delete.
            deleted_by_id:     UUID of dashboard user who deleted (None if CRM deleted).
            is_deleted_by_crm: True if CRM deleted it, False if user deleted it.

        Returns:
            Updated Ticket ORM object.
        """
        ticket.is_deleted = True
        ticket.deleted_at = datetime.utcnow()
        ticket.deleted_by_id = deleted_by_id
        ticket.is_deleted_by_crm = is_deleted_by_crm

        await self.db.flush()
        await self.db.refresh(ticket)
        return ticket

    # ------------------------------------------------------------------
    # WRITE — restore (undo soft delete)
    # ------------------------------------------------------------------

    async def restore(self, ticket: Ticket) -> Ticket:
        """
        Restore a soft-deleted ticket.

        Returns:
            Restored Ticket ORM object.
        """
        ticket.is_deleted = False
        ticket.deleted_at = None
        ticket.deleted_by_id = None
        ticket.is_deleted_by_crm = False

        await self.db.flush()
        await self.db.refresh(ticket)
        return ticket