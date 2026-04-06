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
Post endpoints call add_comment().
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx
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
        result = await self.db.execute(select(Ticket).where(Ticket.id == ticket_id))
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

    async def _get_source_or_500(self, source_system_id: int) -> SourceSystem:
        """Load a SourceSystem row or raise 500 — ticket data is inconsistent if missing."""
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.id == source_system_id)
        )
        source = result.scalars().first()
        if not source:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Ticket has no source system configured",
            )
        return source

    async def _post_to_crm(
        self,
        system_name: str,
        crm_ticket_id: str,
        text: str,
        author_name: str,
    ) -> str:
        """
        Dispatch comment to the correct CRM client.

        Wraps ALL exceptions into clean HTTPExceptions so FastAPI's
        error handler always fires — which means CORS middleware always
        gets to add its headers. A bare unhandled exception produces a
        raw 500 that bypasses middleware, stripping CORS headers and
        confusing the browser into reporting a CORS error.
        """
        try:
            if system_name == "zammad":
                return await self._post_zammad(crm_ticket_id, text, author_name)

            if system_name == "espocrm":
                return await self._post_espo(crm_ticket_id, text, author_name)

            # Unsupported CRM — clean 400, not a 500
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Posting comments not supported for CRM: {system_name}",
            )

        except HTTPException:
            raise  # already a clean HTTP error — let FastAPI handle it

        except httpx.TimeoutException:
            logger.warning(
                "CRM timeout | crm=%s crm_ticket=%s", system_name, crm_ticket_id
            )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"{system_name} timed out. Try again in a moment.",
            )

        except httpx.HTTPStatusError as exc:
            logger.error(
                "CRM HTTP error | crm=%s status=%s body=%.200s",
                system_name,
                exc.response.status_code,
                exc.response.text,
            )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"{system_name} rejected the comment (HTTP {exc.response.status_code}).",
            )

        except Exception:
            # Catch-all: log the full traceback server-side, return a safe
            # 502 to the client. This is what was causing the CORS-less 500 —
            # unhandled exceptions skip middleware entirely.
            logger.exception(
                "Unexpected CRM error | crm=%s crm_ticket=%s",
                system_name,
                crm_ticket_id,
            )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="Could not reach the CRM. The comment was not posted.",
            )

    async def _post_zammad(
        self,
        crm_ticket_id: str,
        text: str,
        author_name: str,
    ) -> str:
        from app.integrations.zammad.client import ZammadClient

        async with ZammadClient() as client:
            response = await client.post_comment(
                crm_ticket_id=crm_ticket_id,
                body=text,
                author_name=author_name,
            )
        return str(response.get("id") or f"local-{uuid.uuid4()}")

    async def _post_espo(
        self,
        crm_ticket_id: str,
        text: str,
        author_name: str,
    ) -> str:
        from app.integrations.espo.client import EspoClient

        async with EspoClient() as client:
            response = await client.post_comment(
                crm_ticket_id=crm_ticket_id,
                body=text,
                author_name=author_name,
            )
        return str(response.get("id") or f"local-{uuid.uuid4()}")

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

        Returns:
            Number of comments upserted.
        """
        from app.integrations.normalizer.comment_normalizer import (
            extract_first_zammad_body,
        )
        from app.integrations.zammad.client import ZammadClient

        source_system_id = await self._get_source_system_id("zammad")

        async with ZammadClient() as client:
            raw = await client.get_comments_by_ticket(crm_ticket_id)

        first_body = extract_first_zammad_body(raw)
        if first_body:
            ticket = await self._get_ticket_or_404(ticket_id)
            ticket.description = first_body
            self.db.add(ticket)
            logger.info(
                "Updated description for ticket %s from first Zammad article", ticket_id
            )

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

        logger.info("Zammad sync: upserted %d comments for ticket %s", count, ticket_id)
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
            "EspoCRM sync: upserted %d comments for ticket %s", count, ticket_id
        )
        return count

    # ------------------------------------------------------------------
    # WRITE — sync comments auto-detecting source from ticket
    # ------------------------------------------------------------------

    async def sync_comments_for_ticket(self, ticket_id: uuid.UUID) -> int:
        """
        Fetch the ticket, determine its source system, then sync comments
        from the correct CRM automatically.

        Returns:
            Total number of comments upserted.
        """
        ticket = await self._get_ticket_or_404(ticket_id)
        source = await self._get_source_or_500(ticket.source_system_id)

        if source.system_name == "zammad":
            return await self.sync_zammad_comments(
                ticket_id=ticket_id,
                crm_ticket_id=ticket.crm_ticket_id,
            )
        if source.system_name == "espocrm":
            return await self.sync_espo_comments(
                ticket_id=ticket_id,
                crm_ticket_id=ticket.crm_ticket_id,
            )

        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Comment sync not supported for source: {source.system_name}",
        )

    # ------------------------------------------------------------------
    # WRITE — post a new comment from the dashboard to the CRM + DB
    # ------------------------------------------------------------------

    async def add_comment(
        self,
        ticket_id: uuid.UUID,
        text: str,
        author_name: str = "Agent",
        author_email: str | None = None,
    ) -> TicketComment:
        """
        Post a new comment to the originating CRM and persist it in our DB.

        Flow:
            1. Load ticket → resolve crm_ticket_id + source system
            2. POST to the correct CRM via _post_to_crm (all errors caught → 502)
            3. Upsert the new comment into our DB
            4. Return the saved TicketComment row

        Raises:
            404 – ticket not found
            400 – unsupported CRM
            502 – CRM rejected or failed to respond
        """
        ticket = await self._get_ticket_or_404(ticket_id)
        source = await self._get_source_or_500(ticket.source_system_id)

        crm_comment_id = await self._post_to_crm(
            system_name=source.system_name,
            crm_ticket_id=ticket.crm_ticket_id,
            text=text,
            author_name=author_name,
        )

        now = datetime.now(timezone.utc)
        comment = await self.repo.upsert(
            ticket_id=ticket_id,
            source_system_id=source.id,  # already loaded — no second DB call
            crm_comment_id=crm_comment_id,
            body=text,
            comment_type="note",
            author_name=author_name,
            author_email=author_email,
            is_internal=False,
            crm_created_at=now,
            crm_updated_at=now,
        )

        logger.info(
            "Comment posted | crm_id=%s ticket=%s crm=%s",
            crm_comment_id,
            ticket_id,
            source.system_name,
        )
        return comment
