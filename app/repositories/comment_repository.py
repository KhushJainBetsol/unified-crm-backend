"""
app/repositories/comment_repository.py

Database queries for the ticket_comments table — no business logic here.

Every read query uses joinedload to avoid N+1 problems.

Loaded relationships per query:
  - TicketComment.source_system → source_systems.system_name
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.ticket_comment import TicketComment

logger = logging.getLogger(__name__)


def _base_query():
    """
    Base SELECT with joinedload on source_system.
    All read queries build on this so the join is never forgotten.
    """
    return select(TicketComment).options(
        joinedload(TicketComment.source_system),
    )


class CommentRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — all comments for a ticket
    # ------------------------------------------------------------------

    async def get_by_ticket_id(
        self,
        ticket_id: uuid.UUID,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[TicketComment], int]:
        """
        Return paginated comments for a single ticket, oldest first.

        Args:
            ticket_id: Internal UUID of the ticket.
            offset:    Records to skip.
            limit:     Max records to return.

        Returns:
            Tuple of (list of TicketComment ORM objects, total count).
        """
        from sqlalchemy import func

        count_q = (
            select(func.count())
            .select_from(TicketComment)
            .where(TicketComment.ticket_id == ticket_id)
        )
        total = (await self.db.execute(count_q)).scalar_one()

        q = (
            _base_query()
            .where(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.crm_created_at.asc().nulls_last())
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(q)
        comments = list(result.scalars().unique().all())

        return comments, total

    # ------------------------------------------------------------------
    # READ — single comment
    # ------------------------------------------------------------------

    async def get_by_id(self, comment_id: uuid.UUID) -> TicketComment | None:
        """Fetch a single comment by internal UUID."""
        q = _base_query().where(TicketComment.id == comment_id)
        result = await self.db.execute(q)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # READ — lookup by CRM id (used during upsert)
    # ------------------------------------------------------------------

    async def get_by_crm_id(
        self,
        crm_comment_id: str,
        source_system_id: int,
    ) -> TicketComment | None:
        """
        Find an existing comment by its CRM id + source system.
        Used by upsert to decide insert vs update.
        """
        q = (
            _base_query()  # fixed: was select(TicketComment), missing joinedload
            .where(
                TicketComment.crm_comment_id == crm_comment_id,
                TicketComment.source_system_id == source_system_id,
            )
        )
        result = await self.db.execute(q)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # WRITE — upsert
    # ------------------------------------------------------------------

    async def upsert(
        self,
        ticket_id: uuid.UUID,
        source_system_id: int,
        crm_comment_id: str,
        body: str | None,
        comment_type: str | None,
        author_name: str | None,
        author_email: str | None,
        is_internal: bool,
        crm_created_at,
        crm_updated_at,
    ) -> TicketComment:
        """
        Insert a new comment or update an existing one.

        Matches on crm_comment_id + source_system_id.
        All fields are updated on every sync run so that edits propagate.

        Returns:
            The inserted or updated TicketComment ORM object,
            with source_system relationship fully loaded.
        """
        existing = await self.get_by_crm_id(crm_comment_id, source_system_id)

        if existing:
            existing.ticket_id      = ticket_id
            existing.body           = body
            existing.comment_type   = comment_type
            existing.author_name    = author_name
            existing.author_email   = author_email
            existing.is_internal    = is_internal
            existing.crm_created_at = crm_created_at
            existing.crm_updated_at = crm_updated_at
            self.db.add(existing)
            logger.debug("Updated comment crm_id=%s", crm_comment_id)
            return existing  # source_system already loaded via get_by_crm_id

        comment = TicketComment(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            source_system_id=source_system_id,
            crm_comment_id=crm_comment_id,
            body=body,
            comment_type=comment_type,
            author_name=author_name,
            author_email=author_email,
            is_internal=is_internal,
            crm_created_at=crm_created_at,
            crm_updated_at=crm_updated_at,
        )
        self.db.add(comment)
        await self.db.flush()                              
        await self.db.refresh(comment, ["source_system"]) 
        logger.debug("Inserted comment crm_id=%s", crm_comment_id)
        return comment

    # ------------------------------------------------------------------
    # WRITE — delete all comments for a ticket (used before re-sync)
    # ------------------------------------------------------------------

    async def delete_by_ticket_id(self, ticket_id: uuid.UUID) -> int:
        """
        Hard-delete all comments for a ticket.
        Returns the number of rows deleted.
        """
        from sqlalchemy import delete

        result = await self.db.execute(
            delete(TicketComment).where(TicketComment.ticket_id == ticket_id)
        )
        return result.rowcount