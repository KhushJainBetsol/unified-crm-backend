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
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.ticket import Ticket


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


class TicketRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list
    # ------------------------------------------------------------------
    async def get_all(
        self,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """
        Fetch a paginated list of tickets with total count.

        Args:
            include_deleted: If False (default) excludes soft-deleted tickets.
            offset:          Number of records to skip.
            limit:           Max records to return.

        Returns:
            Tuple of (list of Ticket ORM objects, total count).
        """
        query = _base_query()

        if not include_deleted:
            query = query.where(Ticket.is_deleted == False)  # noqa: E712

        # total count (separate query without pagination)
        count_query = select(func.count()).select_from(Ticket)
        if not include_deleted:
            count_query = count_query.where(Ticket.is_deleted == False)  # noqa: E712

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # paginated results
        query = query.offset(offset).limit(limit).order_by(Ticket.created_at.desc())
        result = await self.db.execute(query)
        tickets = list(result.scalars().unique().all())

        return tickets, total

    async def get_by_source_system(
        self,
        source_system_id: int,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """
        Fetch tickets belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            include_deleted:  If False excludes soft-deleted tickets.
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Ticket ORM objects, total count).
        """
        query = _base_query().where(
            Ticket.source_system_id == source_system_id
        )
        count_query = select(func.count()).select_from(Ticket).where(
            Ticket.source_system_id == source_system_id
        )

        if not include_deleted:
            query = query.where(Ticket.is_deleted == False)  # noqa: E712
            count_query = count_query.where(Ticket.is_deleted == False)  # noqa: E712

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Ticket.created_at.desc())
        result = await self.db.execute(query)
        tickets = list(result.scalars().unique().all())

        return tickets, total

    # ------------------------------------------------------------------
    # READ — single
    # ------------------------------------------------------------------
    async def get_by_id(self, ticket_id: uuid.UUID) -> Ticket | None:
        """
        Fetch a single ticket by internal UUID.

        Returns:
            Ticket ORM object or None if not found.
        """
        query = _base_query().where(Ticket.id == ticket_id)
        result = await self.db.execute(query)
        return result.scalars().first()

    async def get_by_crm_id(
        self,
        crm_ticket_id: str,
        source_system_id: int,
    ) -> Ticket | None:
        """
        Fetch a ticket by its original CRM ID + source system.
        Used by the sync service to check if a ticket already exists
        before deciding to insert or update.

        Args:
            crm_ticket_id:   Original ticket ID from the CRM.
            source_system_id: FK id of the source system.

        Returns:
            Ticket ORM object or None.
        """
        query = _base_query().where(
            Ticket.crm_ticket_id == crm_ticket_id,
            Ticket.source_system_id == source_system_id,
        )
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
                  Must include all required fields.

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
    ) -> tuple[Ticket, bool]:
        """
        Insert if not exists, update if exists.
        The sync service calls this for every ticket from the CRM.

        Args:
            crm_ticket_id:    Original CRM ticket ID.
            source_system_id: FK id of the source system.
            data:             Full dict of ticket field values.

        Returns:
            Tuple of (Ticket ORM object, created: bool).
            created=True means it was inserted, False means updated.
        """
        existing = await self.get_by_crm_id(crm_ticket_id, source_system_id)

        if existing:
            updated = await self.update(existing, data)
            return updated, False
        else:
            data["crm_ticket_id"] = crm_ticket_id
            data["source_system_id"] = source_system_id
            created = await self.create(data)
            return created, True

    # ------------------------------------------------------------------
    # WRITE — soft delete
    # ------------------------------------------------------------------
    async def soft_delete(
        self,
        ticket: Ticket,
        deleted_by_id: uuid.UUID | None,
        deleted_by_source: bool,
    ) -> Ticket:
        """
        Soft-delete a ticket by setting is_deleted + deleted_at.

        Args:
            ticket:            Ticket ORM object to delete.
            deleted_by_id:     UUID of dashboard user who deleted (None if CRM deleted).
            deleted_by_source: True if CRM deleted it, False if user deleted it.

        Returns:
            Updated Ticket ORM object.
        """
        ticket.is_deleted = True
        ticket.deleted_at = datetime.utcnow()
        ticket.deleted_by_id = deleted_by_id
        ticket.deleted_by_source = deleted_by_source

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
        ticket.deleted_by_source = False

        await self.db.flush()
        await self.db.refresh(ticket)
        return ticket