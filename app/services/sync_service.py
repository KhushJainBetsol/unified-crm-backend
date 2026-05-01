"""
app/services/sync_service.py

Sync service — saves NormalizedTickets into the database.

Multitenancy:
  Every sync run is now scoped to a single (tenant_id, source_system_id) pair.
  tenant_id is required — it is stamped on every ticket, agent, customer,
  and company upserted during the run.

CRM ID → Internal UUID resolution:
  Zammad   → integer IDs  (owner_id=4, customer_id=8, organization_id=2)
  EspoCRM  → string UUIDs (assignedUserId="abc123", contactId="xyz", accountId="aaa")

  These raw CRM IDs are stored as crm_agent_id / crm_customer_id / crm_company_id
  in the normalizer output. The sync service resolves them to internal UUIDs by
  querying agents / customers / companies tables using:
    (tenant_id, crm_*_id, source_system_id) → internal UUID

  All resolutions are cached in-memory per sync run to avoid N+1 DB queries.

  If an agent / customer / company is not yet in the DB the field is set to NULL
  and a warning is logged. Run the entity sync first to populate those tables.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.normalizer.schema import NormalizedTicket
from app.models.agent import Agent
from app.models.company import Company
from app.models.customer import Customer
from app.models.source_system import SourceSystem
from app.models.ticket_priority import TicketPriority
from app.models.ticket_status import TicketStatus
from app.repositories.ticket_repository import TicketRepository

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    source_system: str
    tenant_id: uuid.UUID
    total_fetched: int
    created: int
    updated: int
    failed: int
    deleted: int = 0


class SyncService:

    def __init__(self, db: AsyncSession) -> None:
        self.db   = db
        self.repo = TicketRepository(db)

        # In-memory caches — populated on first lookup, reused per sync run.
        # Agent/customer/company keys include tenant_id to be safe when the
        # same SyncService instance is (mistakenly) reused across tenants.
        self._status_cache:   dict[str, int]                                   = {}
        self._priority_cache: dict[str, int]                                   = {}
        self._agent_cache:    dict[tuple[uuid.UUID, str, int], uuid.UUID | None] = {}
        self._customer_cache: dict[tuple[uuid.UUID, str, int], uuid.UUID | None] = {}
        self._company_cache:  dict[tuple[uuid.UUID, str, int], uuid.UUID | None] = {}

    # ------------------------------------------------------------------
    # Source system
    # ------------------------------------------------------------------
    async def _get_source_system_id(self, name: str) -> int | None:
        try:
            result = await self.db.execute(
                select(SourceSystem).where(SourceSystem.system_name == name.lower())
            )
            source = result.scalars().first()
            if not source:
                logger.error(
                    "Source system '%s' not found in DB — make sure it is seeded", name
                )
            return source.id if source else None
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "DB error while resolving source system '%s': %s", name, exc
            )
            return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def _get_status_id(self, name: str) -> int | None:
        if name in self._status_cache:
            return self._status_cache[name]
        try:
            result = await self.db.execute(
                select(TicketStatus).where(TicketStatus.status_name == name.lower())
            )
            status = result.scalars().first()
            if not status:
                logger.warning("Status '%s' not found — falling back to 'open'", name)
                result = await self.db.execute(
                    select(TicketStatus).where(TicketStatus.status_name == "open")
                )
                status = result.scalars().first()
            if status:
                self._status_cache[name] = status.id
                return status.id
            logger.error("Fallback status 'open' not found in DB — check seed data")
            return None
        except Exception as exc:
            await self.db.rollback()
            logger.exception("DB error while resolving status '%s': %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Priority
    # ------------------------------------------------------------------
    async def _get_priority_id(self, name: str | None) -> int | None:
        if not name:
            return None
        if name in self._priority_cache:
            return self._priority_cache[name]
        try:
            result = await self.db.execute(
                select(TicketPriority).where(
                    TicketPriority.priority_name == name.lower()
                )
            )
            priority = result.scalars().first()
            if priority:
                self._priority_cache[name] = priority.id
                return priority.id
            logger.warning("Priority '%s' not found — priority_id will be NULL", name)
            return None
        except Exception as exc:
            await self.db.rollback()
            logger.exception("DB error while resolving priority '%s': %s", name, exc)
            return None

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------
    async def _get_agent_uuid(
        self,
        crm_agent_id: str | None,
        source_system_id: int,
        tenant_id: uuid.UUID,
    ) -> uuid.UUID | None:
        if not crm_agent_id:
            return None
        cache_key = (tenant_id, crm_agent_id, source_system_id)
        if cache_key in self._agent_cache:
            return self._agent_cache[cache_key]
        try:
            result = await self.db.execute(
                select(Agent).where(
                    Agent.tenant_id        == tenant_id,
                    Agent.crm_agent_id     == crm_agent_id,
                    Agent.source_system_id == source_system_id,
                )
            )
            agent       = result.scalars().first()
            internal_id = agent.id if agent else None
            self._agent_cache[cache_key] = internal_id
            if not internal_id:
                logger.warning(
                    "Agent crm_id=%r source=%d tenant=%s not found — agent_id will be NULL.",
                    crm_agent_id, source_system_id, tenant_id,
                )
            return internal_id
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "DB error while resolving agent crm_id=%r source=%d tenant=%s: %s",
                crm_agent_id, source_system_id, tenant_id, exc,
            )
            return None

    # ------------------------------------------------------------------
    # Customer
    # ------------------------------------------------------------------
    async def _get_customer_uuid(
        self,
        crm_customer_id: str | None,
        source_system_id: int,
        tenant_id: uuid.UUID,
    ) -> uuid.UUID | None:
        if not crm_customer_id:
            return None
        cache_key = (tenant_id, crm_customer_id, source_system_id)
        if cache_key in self._customer_cache:
            return self._customer_cache[cache_key]
        try:
            result = await self.db.execute(
                select(Customer).where(
                    Customer.tenant_id        == tenant_id,
                    Customer.crm_customer_id  == crm_customer_id,
                    Customer.source_system_id == source_system_id,
                )
            )
            customer    = result.scalars().first()
            internal_id = customer.id if customer else None
            self._customer_cache[cache_key] = internal_id
            if not internal_id:
                logger.warning(
                    "Customer crm_id=%r source=%d tenant=%s not found — customer_id will be NULL.",
                    crm_customer_id, source_system_id, tenant_id,
                )
            return internal_id
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "DB error while resolving customer crm_id=%r source=%d tenant=%s: %s",
                crm_customer_id, source_system_id, tenant_id, exc,
            )
            return None

    # ------------------------------------------------------------------
    # Company
    # ------------------------------------------------------------------
    async def _get_company_uuid(
        self,
        crm_company_id: str | None,
        source_system_id: int,
        tenant_id: uuid.UUID,
    ) -> uuid.UUID | None:
        if not crm_company_id:
            return None
        cache_key = (tenant_id, crm_company_id, source_system_id)
        if cache_key in self._company_cache:
            return self._company_cache[cache_key]
        try:
            result = await self.db.execute(
                select(Company).where(
                    Company.tenant_id        == tenant_id,
                    Company.crm_company_id   == crm_company_id,
                    Company.source_system_id == source_system_id,
                )
            )
            company     = result.scalars().first()
            internal_id = company.id if company else None
            self._company_cache[cache_key] = internal_id
            if not internal_id:
                logger.warning(
                    "Company crm_id=%r source=%d tenant=%s not found — company_id will be NULL.",
                    crm_company_id, source_system_id, tenant_id,
                )
            return internal_id
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "DB error while resolving company crm_id=%r source=%d tenant=%s: %s",
                crm_company_id, source_system_id, tenant_id, exc,
            )
            return None

    # ------------------------------------------------------------------
    # Save single ticket (wrapped in a SAVEPOINT)
    # ------------------------------------------------------------------
    async def _save_ticket(
        self,
        ticket: NormalizedTicket,
        source_system_id: int,
        tenant_id: uuid.UUID,
        status_id: int,
        priority_id: int | None,
        agent_id: uuid.UUID | None,
        customer_id: uuid.UUID | None,
        company_id: uuid.UUID | None,
    ) -> bool:
        """
        Upsert one normalized ticket inside a SAVEPOINT.
        Returns True if created, False if updated.
        """
        data = {
            "tenant_id":        tenant_id,
            "title":            ticket.title,
            "description":      ticket.description,
            "status_id":        status_id,
            "priority_id":      priority_id,
            "agent_id":         agent_id,
            "customer_id":      customer_id,
            "company_id":       company_id,
            "created_at":       ticket.created_at,
            "updated_at":       ticket.updated_at,
            "closed_at":        ticket.closed_at,
            "is_deleted":       False,
            "is_deleted_by_crm": False,
        }

        async with self.db.begin_nested():
            _, created = await self.repo.upsert(
                crm_ticket_id    = ticket.crm_ticket_id,
                source_system_id = source_system_id,
                tenant_id        = tenant_id,
                data             = data,
            )
        return created

    # ------------------------------------------------------------------
    # Main sync entry point
    # ------------------------------------------------------------------
    async def sync_tickets(
        self,
        normalized_tickets: list[NormalizedTicket],
        source_system: str,
        tenant_id: uuid.UUID,
        crm_ids_to_delete: set[str] | None = None,
    ) -> SyncResult:
        """
        Resolve all FK IDs and upsert a list of NormalizedTickets into the DB.
        After upserting, soft-delete any tickets in crm_ids_to_delete.

        Args:
            normalized_tickets: Tickets pre-filtered for this tenant's org.
            source_system:      e.g. "zammad" or "espocrm".
            tenant_id:          The tenant these tickets belong to.
            crm_ids_to_delete:  Optional set of CRM ticket IDs to soft-delete (tickets no longer in CRM).

        Resolution order per ticket:
          1. status_id    — from ticket_status table (required, falls back to open)
          2. priority_id  — from ticket_priority table (optional)
          3. agent_id     — from agents table by (tenant_id, crm_agent_id, source_system_id)
          4. customer_id  — from customers table by (tenant_id, crm_customer_id, source_system_id)
          5. company_id   — from companies table by (tenant_id, crm_company_id, source_system_id)
        """
        result = SyncResult(
            source_system = source_system,
            tenant_id     = tenant_id,
            total_fetched = len(normalized_tickets),
            created       = 0,
            updated       = 0,
            failed        = 0,
            deleted       = 0,
        )

        if not normalized_tickets:
            logger.info("No tickets to sync for %s tenant=%s", source_system, tenant_id)
            return result

        source_system_id = await self._get_source_system_id(source_system)
        if not source_system_id:
            result.failed = len(normalized_tickets)
            logger.error(
                "Aborting sync for '%s' tenant=%s — source system could not be resolved",
                source_system, tenant_id,
            )
            return result

        logger.info(
            "Syncing %d tickets from %s (source_system_id=%d, tenant_id=%s)",
            len(normalized_tickets), source_system, source_system_id, tenant_id,
        )

        for ticket in normalized_tickets:
            try:
                status_id = await self._get_status_id(ticket.status)
                if not status_id:
                    logger.error(
                        "Skipping ticket %s — could not resolve status '%s'",
                        ticket.crm_ticket_id, ticket.status,
                    )
                    result.failed += 1
                    continue

                priority_id = await self._get_priority_id(ticket.priority)
                agent_id    = await self._get_agent_uuid(
                    ticket.crm_agent_id, source_system_id, tenant_id
                )
                customer_id = await self._get_customer_uuid(
                    ticket.crm_customer_id, source_system_id, tenant_id
                )
                company_id  = await self._get_company_uuid(
                    ticket.crm_company_id, source_system_id, tenant_id
                )

                created = await self._save_ticket(
                    ticket           = ticket,
                    source_system_id = source_system_id,
                    tenant_id        = tenant_id,
                    status_id        = status_id,
                    priority_id      = priority_id,
                    agent_id         = agent_id,
                    customer_id      = customer_id,
                    company_id       = company_id,
                )

                if created:
                    result.created += 1
                else:
                    result.updated += 1

            except Exception as exc:
                await self.db.rollback()
                logger.error(
                    "Failed to save ticket %s tenant=%s: %s",
                    ticket.crm_ticket_id, tenant_id, exc,
                )
                result.failed += 1

        # ── Handle ticket deletions (tickets in DB but no longer in CRM) ────────────────
        if crm_ids_to_delete:
            for crm_id in crm_ids_to_delete:
                try:
                    existing = await self.repo.get_by_crm_id(
                        crm_id, source_system_id, tenant_id=tenant_id
                    )
                    if existing and not existing.is_deleted:
                        await self.repo.soft_delete(
                            ticket=existing,
                            deleted_by_id=None,
                            is_deleted_by_crm=True,
                        )
                        result.deleted += 1
                        logger.info(
                            "Sync: soft-deleted orphaned ticket | crm_ticket_id=%s | no longer in %s",
                            crm_id, source_system,
                        )
                except Exception as exc:
                    await self.db.rollback()
                    logger.error(
                        "Failed to delete ticket %s tenant=%s: %s",
                        crm_id, tenant_id, exc,
                    )

        logger.info(
            "Sync complete for %s tenant=%s — created: %d, updated: %d, deleted: %d, failed: %d",
            source_system, tenant_id, result.created, result.updated, result.deleted, result.failed,
        )
        return result