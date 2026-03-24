"""
app/services/comment_service.py

Business logic for ticket comments.

Responsibilities:
  - Fetch raw comments from CRM clients (Zammad + EspoCRM)
  - Normalize them via comment_normalizer
  - Resolve ticket CRM id → internal UUID
  - Resolve source system name → DB id
  - Upsert normalized comments into the DB via CommentRepository
  - Paginated reads for the API layer

Routes call get_comments_for_ticket().
Sync endpoints call sync_comments_for_ticket().
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.normalizer.comment_normalizer import (
    normalize_espo_comments,
    normalize_zammad_comments,
)
from app.models.source_system import SourceSystem
from app.models.ticket import Ticket
from app.models.ticket_comment import TicketComment
from app.repositories.comment_repository import CommentRepository

logger = logging.getLogger(__name__)


class CommentService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CommentRepository(db)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_ticket_or_404(self, ticket_id: uuid.UUID) -> Ticket:
        result = await self.db.execute(
            select(Ticket).where(Ticket.id == ticket_id)
        )
        ticket = result.scalars().first()
        if not ticket:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found",
            )
        return ticket

    async def _get_source_system_id(self, name: str) -> int:
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.system_name == name.lower())
        )
        row = result.scalars().first()
        if not row:
            raise ValueError(f"Source system '{name}' not in DB")
        return row.id

    # ------------------------------------------------------------------
    # READ — paginated list for API
    # ------------------------------------------------------------------

    async def get_comments_for_ticket(
        self,
        ticket_id: uuid.UUID,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[TicketComment], int]:
        """
        Return paginated comments for a ticket.
        Raises HTTP 404 if the ticket doesn't exist.
        """
        await self._get_ticket_or_404(ticket_id)
        offset = (page - 1) * page_size
        return await self.repo.get_by_ticket_id(
            ticket_id=ticket_id,
            offset=offset,
            limit=page_size,
        )

    # ------------------------------------------------------------------
    # WRITE — sync comments from Zammad for one ticket
    # ------------------------------------------------------------------

    async def sync_zammad_comments(
        self,
        ticket_id: uuid.UUID,
        crm_ticket_id: str,
    ) -> int:
        """
        Fetch all articles from Zammad for crm_ticket_id,
        normalize them, and upsert into the DB.

        Args:
            ticket_id:     Internal UUID of the ticket in our DB.
            crm_ticket_id: Zammad's integer ticket id (as string).

        Returns:
            Number of comments upserted.
        """
        from app.integrations.zammad.client import ZammadClient

        source_system_id = await self._get_source_system_id("zammad")

        async with ZammadClient() as client:
            raw = await client.get_comments_by_ticket(crm_ticket_id)

        normalized = normalize_zammad_comments(raw)

        count = 0
        for comment in normalized:
            await self.repo.upsert(
                ticket_id=ticket_id,
                source_system_id=source_system_id,
                crm_comment_id=comment.crm_comment_id,
                body=comment.body,
                comment_type=comment.comment_type,
                author_name=comment.author_name,
                author_email=comment.author_email,
                is_internal=comment.is_internal,
                crm_created_at=comment.crm_created_at,
                crm_updated_at=comment.crm_updated_at,
            )
            count += 1

        logger.info(
            "Zammad comment sync: upserted %d comments for ticket %s",
            count, ticket_id,
        )
        return count

    # ------------------------------------------------------------------
    # WRITE — sync comments from EspoCRM for one ticket
    # ------------------------------------------------------------------

    async def sync_espo_comments(
        self,
        ticket_id: uuid.UUID,
        crm_ticket_id: str,
    ) -> int:
        """
        Fetch all stream Posts from EspoCRM for crm_ticket_id,
        normalize them, and upsert into the DB.

        Args:
            ticket_id:     Internal UUID of the ticket in our DB.
            crm_ticket_id: EspoCRM Case UUID string.

        Returns:
            Number of comments upserted.
        """
        from app.integrations.espo.client import EspoClient

        source_system_id = await self._get_source_system_id("espocrm")

        async with EspoClient() as client:
            raw = await client.get_comments_by_ticket(crm_ticket_id)

        normalized = normalize_espo_comments(raw)

        count = 0
        for comment in normalized:
            await self.repo.upsert(
                ticket_id=ticket_id,
                source_system_id=source_system_id,
                crm_comment_id=comment.crm_comment_id,
                body=comment.body,
                comment_type=comment.comment_type,
                author_name=comment.author_name,
                author_email=comment.author_email,
                is_internal=comment.is_internal,
                crm_created_at=comment.crm_created_at,
                crm_updated_at=comment.crm_updated_at,
            )
            count += 1

        logger.info(
            "EspoCRM comment sync: upserted %d comments for ticket %s",
            count, ticket_id,
        )
        return count

    # ------------------------------------------------------------------
    # WRITE — sync comments auto-detecting source from ticket
    # ------------------------------------------------------------------

    async def sync_comments_for_ticket(self, ticket_id: uuid.UUID) -> int:
        """
        Fetch the ticket, determine its source system, then sync comments
        from the correct CRM automatically.

        This is what the sync endpoint calls — no need to pass source manually.

        Returns:
            Total number of comments upserted.
        """
        ticket = await self._get_ticket_or_404(ticket_id)

        # Load source system name
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.id == ticket.source_system_id)
        )
        source = result.scalars().first()
        if not source:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Ticket has no source system",
            )

        if source.system_name == "zammad":
            return await self.sync_zammad_comments(
                ticket_id=ticket_id,
                crm_ticket_id=ticket.crm_ticket_id,
            )
        elif source.system_name == "espocrm":
            return await self.sync_espo_comments(
                ticket_id=ticket_id,
                crm_ticket_id=ticket.crm_ticket_id,
            )
        else:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Comment sync not supported for source: {source.system_name}",
            )
