"""
app/services/comment_service.py

Business logic for ticket comments.

Responsibilities:
  - Sync comments from CRM via the adapter pattern (fetch_comments)
  - Normalize UnifiedComment → repo.upsert
  - Resolve ticket → integration_id via TenantSourceSystem
  - Paginated reads for the API layer
  - Post new comments to CRM + DB

Sync flow (adapter pattern):
  sync_comments_for_ticket(ticket_id, factory)
    → load ticket + source system
    → _get_integration_id_for_ticket  (tenant_id + source_system_id → integration_id)
    → factory.create(integration_id)  → correct adapter
    → adapter.fetch_comments(crm_ticket_id)  → List[UnifiedComment]
    → Zammad only: is_first_article=True → update ticket.description
    → remaining items → repo.upsert

Routes call get_comments_for_ticket().
Sync endpoints call sync_comments_for_ticket().
Post endpoints call add_comment().
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import UnifiedComment
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory
from app.models.source_system import SourceSystem
from app.models.tenant_source_systems import TenantSourceSystem
from app.models.ticket import Ticket
from app.models.ticket_comment import TicketComment
from app.repositories.comment_repository import CommentRepository

logger = logging.getLogger(__name__)


class CommentService:

    def __init__(self, db: AsyncSession) -> None:
        self.db   = db
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

    async def _get_integration_id_for_ticket(self, ticket: Ticket) -> str:
        """
        Resolve the integration_id for a ticket via TenantSourceSystem.

        Uses ticket.tenant_id + ticket.source_system_id to look up the
        active TenantSourceSystem row, then returns its integration_id
        as a string so the adapter factory can create the right adapter.

        Raises:
            HTTP 500 — ticket has no tenant_id (cannot route to an adapter)
            HTTP 404 — no active integration found for this tenant + source system
        """
        if ticket.tenant_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Ticket {ticket.id} has no tenant_id — "
                    "cannot resolve CRM integration for comment sync."
                ),
            )

        result = await self.db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.tenant_id        == ticket.tenant_id,
                TenantSourceSystem.source_system_id == ticket.source_system_id,
                TenantSourceSystem.is_active        == True,  # noqa: E712
            )
        )
        tss = result.scalars().first()
        if not tss:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No active CRM integration found for "
                    f"tenant={ticket.tenant_id} "
                    f"source_system_id={ticket.source_system_id}. "
                    "Make sure the integration is registered and active."
                ),
            )
        return str(tss.integration_id)

    async def _post_to_crm(
        self,
        system_name: str,
        crm_ticket_id: str,
        text: str,
        author_name: str,
    ) -> str:
        """
        Dispatch a new comment to the correct CRM client.

        Wraps ALL exceptions into clean HTTPExceptions so FastAPI's
        error handler always fires — which means CORS middleware always
        gets to add its headers.

        Note: posting comments still uses the legacy direct clients
        (ZammadClient / EspoClient) because push_comment has not yet
        been added to the adapter interface.  This can be migrated in a
        follow-up once fetch_comments is stable.
        """
        try:
            if system_name == "zammad":
                return await self._post_zammad(crm_ticket_id, text, author_name)

            if system_name == "espocrm":
                return await self._post_espo(crm_ticket_id, text, author_name)

            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Posting comments not supported for CRM: {system_name}",
            )

        except HTTPException:
            raise

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
    # WRITE — sync comments via adapter (adapter pattern)
    # ------------------------------------------------------------------

    async def sync_comments_for_ticket(
        self,
        ticket_id: uuid.UUID,
        factory: CrmAdapterFactory | None = None,
    ) -> int:
        """
        Fetch comments from the ticket's source CRM via the adapter pattern
        and upsert them into the DB.

        Automatically detects the source system from the ticket record —
        no CRM type needs to be passed by the caller.

        Flow:
            1. Load ticket → source system name + crm_ticket_id
            2. Resolve integration_id via TenantSourceSystem
               (tenant_id + source_system_id → integration_id)
            3. factory.create(integration_id) → correct adapter
            4. adapter.fetch_comments(crm_ticket_id) → List[UnifiedComment]
            5. Zammad only: article with is_first_article=True updates
               ticket.description (opening message) and is skipped as a
               comment row — same behaviour as the old sync_zammad_comments
            6. Remaining UnifiedComment items → repo.upsert

        Args:
            ticket_id: Internal UUID of the ticket to sync comments for.
            factory:   CrmAdapterFactory instance. If None, a default
                       factory is constructed from app DI dependencies.
                       Pass explicitly from routes for testability.

        Returns:
            Total number of comment rows upserted.

        Raises:
            HTTP 404 — ticket not found or no active integration
            HTTP 500 — ticket has no tenant_id
            HTTP 502 — CRM unreachable or rejected the request
            HTTP 400 — source system does not support comment sync
        """
        ticket = await self._get_ticket_or_404(ticket_id)
        source = await self._get_source_or_500(ticket.source_system_id)

        # Unsupported source systems fail fast before hitting the network
        supported = {"zammad", "espocrm"}
        if source.system_name not in supported:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Comment sync not supported for source: {source.system_name}",
            )

        integration_id = await self._get_integration_id_for_ticket(ticket)

        # Allow callers to inject a factory (e.g. from FastAPI DI or tests).
        # If not provided, build one using the app's default DI wiring.
        if factory is None:
            from app.adapter_dependencies.deps import get_adapter_factory_instance
            factory = get_adapter_factory_instance()

        try:
            adapter = await factory.create(integration_id)
            async with adapter:
                result = await adapter.fetch_comments(ticket.crm_ticket_id)
        except AdapterFactoryError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"CRM connection failed: {exc}",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error fetching comments via adapter "
                "ticket=%s integration=%s",
                ticket_id,
                integration_id,
            )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"CRM connection failed: {exc}",
            ) from exc

        comments: list[UnifiedComment] = result.items

        # Zammad: the oldest article is the ticket's opening message.
        # Update ticket.description from it instead of storing it as a comment.
        if source.system_name == "zammad":
            first_body: str | None = next(
                (c.body for c in comments if c.is_first_article),
                None,
            )
            if first_body:
                ticket.description = first_body
                self.db.add(ticket)
                logger.info(
                    "Updated description for ticket %s from first Zammad article",
                    ticket_id,
                )

        count = 0
        for comment in comments:
            # Skip the description article — already handled above
            if comment.is_first_article:
                continue

            await self.repo.upsert(
                ticket_id        = ticket_id,
                source_system_id = source.id,
                crm_comment_id   = comment.id,
                body             = comment.body,
                comment_type     = comment.comment_type,
                author_name      = comment.author_name,
                author_email     = comment.author_email,
                is_internal      = comment.is_internal,
                crm_created_at   = comment.created_at,
                crm_updated_at   = comment.updated_at,
            )
            count += 1

        logger.info(
            "%s comment sync: upserted %d comment(s) for ticket %s",
            source.system_name,
            count,
            ticket_id,
        )
        return count

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
            4. Re-fetch with joinedload to guarantee source_system is loaded
            5. Return the saved TicketComment row

        Raises:
            404 – ticket not found
            400 – unsupported CRM
            502 – CRM rejected or failed to respond
        """
        ticket = await self._get_ticket_or_404(ticket_id)
        source = await self._get_source_or_500(ticket.source_system_id)

        crm_comment_id = await self._post_to_crm(
            system_name   = source.system_name,
            crm_ticket_id = ticket.crm_ticket_id,
            text          = text,
            author_name   = author_name,
        )

        now = datetime.now(timezone.utc)
        comment = await self.repo.upsert(
            ticket_id        = ticket_id,
            source_system_id = source.id,
            crm_comment_id   = crm_comment_id,
            body             = text,
            comment_type     = "note",
            author_name      = author_name,
            author_email     = author_email,
            is_internal      = False,
            crm_created_at   = now,
            crm_updated_at   = now,
        )

        comment.source_system = source

        logger.info(
            "Comment posted | crm_id=%s ticket=%s crm=%s",
            crm_comment_id,
            ticket_id,
            source.system_name,
        )
        return comment