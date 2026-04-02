# """
# app/services/sync_service.py

# Sync service — saves NormalizedTickets into the database.

# CRM ID → Internal UUID resolution:
#   Every CRM stores its entities with its own ID format:
#     Zammad   → integer IDs  (owner_id=4, customer_id=8, organization_id=2)
#     EspoCRM  → string UUIDs (assignedUserId="abc123", contactId="xyz", accountId="aaa")

#   These raw CRM IDs are stored as crm_agent_id / crm_customer_id / crm_company_id
#   in the normalizer output. The sync service resolves them to internal UUIDs by
#   querying agents / customers / companies tables using:
#     (crm_*_id, source_system_id) → internal UUID

#   All resolutions are cached in-memory per sync run to avoid N+1 DB queries.

#   If an agent / customer / company is not yet in the DB the field is set to NULL
#   and a warning is logged. Run the respective sync first to populate those tables.
# """

# from __future__ import annotations

# import logging
# import uuid
# from dataclasses import dataclass

# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.integrations.normalizer.schema import NormalizedTicket
# from app.models.agent import Agent
# from app.models.company import Company
# from app.models.customer import Customer
# from app.models.source_system import SourceSystem
# from app.models.ticket_priority import TicketPriority
# from app.models.ticket_status import TicketStatus
# from app.repositories.ticket_repository import TicketRepository

# logger = logging.getLogger(__name__)


# @dataclass
# class SyncResult:
#     source_system: str
#     total_fetched: int
#     created: int
#     updated: int
#     failed: int


# class SyncService:

#     def __init__(self, db: AsyncSession) -> None:
#         self.db = db
#         self.repo = TicketRepository(db)

#         # ----------------------------------------------------------------
#         # In-memory caches — populated on first lookup, reused per sync run
#         # key pattern: (crm_id_string, source_system_id) → internal UUID
#         # ----------------------------------------------------------------
#         self._status_cache:   dict[str, int]                  = {}
#         self._priority_cache: dict[str, int]                  = {}
#         self._agent_cache:    dict[tuple[str, int], uuid.UUID | None] = {}
#         self._customer_cache: dict[tuple[str, int], uuid.UUID | None] = {}
#         self._company_cache:  dict[tuple[str, int], uuid.UUID | None] = {}

#     # ------------------------------------------------------------------
#     # Source system
#     # ------------------------------------------------------------------
#     async def _get_source_system_id(self, name: str) -> int | None:
#         result = await self.db.execute(
#             select(SourceSystem).where(SourceSystem.system_name == name.lower())
#         )
#         source = result.scalars().first()
#         if not source:
#             logger.error(
#                 "Source system '%s' not found in DB — make sure it is seeded", name
#             )
#         return source.id if source else None

#     # ------------------------------------------------------------------
#     # Status
#     # ------------------------------------------------------------------
#     async def _get_status_id(self, name: str) -> int | None:
#         if name in self._status_cache:
#             return self._status_cache[name]

#         result = await self.db.execute(
#             select(TicketStatus).where(TicketStatus.status_name == name.lower())
#         )
#         status = result.scalars().first()

#         if not status:
#             logger.warning("Status '%s' not found — falling back to 'open'", name)
#             result = await self.db.execute(
#                 select(TicketStatus).where(TicketStatus.status_name == "open")
#             )
#             status = result.scalars().first()

#         if status:
#             self._status_cache[name] = status.id
#             return status.id
#         return None

#     # ------------------------------------------------------------------
#     # Priority
#     # ------------------------------------------------------------------
#     async def _get_priority_id(self, name: str | None) -> int | None:
#         if not name:
#             return None
#         if name in self._priority_cache:
#             return self._priority_cache[name]

#         result = await self.db.execute(
#             select(TicketPriority).where(
#                 TicketPriority.priority_name == name.lower()
#             )
#         )
#         priority = result.scalars().first()

#         if priority:
#             self._priority_cache[name] = priority.id
#             return priority.id
#         return None

#     # ------------------------------------------------------------------
#     # Agent — crm_agent_id is a string regardless of CRM
#     #   Zammad:  owner_id     = 4        → stored as "4"
#     #   EspoCRM: assignedUserId = "abc"  → stored as "abc"
#     # ------------------------------------------------------------------
#     async def _get_agent_uuid(
#         self,
#         crm_agent_id: str | None,
#         source_system_id: int,
#     ) -> uuid.UUID | None:
#         if not crm_agent_id:
#             return None

#         cache_key = (crm_agent_id, source_system_id)
#         if cache_key in self._agent_cache:
#             return self._agent_cache[cache_key]

#         result = await self.db.execute(
#             select(Agent).where(
#                 Agent.crm_agent_id == crm_agent_id,
#                 Agent.source_system_id == source_system_id,
#             )
#         )
#         agent = result.scalars().first()

#         internal_id = agent.id if agent else None
#         self._agent_cache[cache_key] = internal_id

#         if not internal_id:
#             logger.warning(
#                 "Agent crm_id=%r source=%d not found — "
#                 "agent_id will be NULL. Sync agents first.",
#                 crm_agent_id, source_system_id,
#             )
#         return internal_id

#     # ------------------------------------------------------------------
#     # Customer — crm_customer_id is a string regardless of CRM
#     #   Zammad:  customer_id  = 8        → stored as "8"
#     #   EspoCRM: contactId    = "xyz"    → stored as "xyz"
#     # ------------------------------------------------------------------
#     async def _get_customer_uuid(
#         self,
#         crm_customer_id: str | None,
#         source_system_id: int,
#     ) -> uuid.UUID | None:
#         if not crm_customer_id:
#             return None

#         cache_key = (crm_customer_id, source_system_id)
#         if cache_key in self._customer_cache:
#             return self._customer_cache[cache_key]

#         result = await self.db.execute(
#             select(Customer).where(
#                 Customer.crm_customer_id == crm_customer_id,
#                 Customer.source_system_id == source_system_id,
#             )
#         )
#         customer = result.scalars().first()

#         internal_id = customer.id if customer else None
#         self._customer_cache[cache_key] = internal_id

#         if not internal_id:
#             logger.warning(
#                 "Customer crm_id=%r source=%d not found — "
#                 "customer_id will be NULL. Sync customers first.",
#                 crm_customer_id, source_system_id,
#             )
#         return internal_id

#     # ------------------------------------------------------------------
#     # Company — crm_company_id is a string regardless of CRM
#     #   Zammad:  organization_id = 2     → stored as "2"
#     #   EspoCRM: accountId       = "aaa" → stored as "aaa"
#     # ------------------------------------------------------------------
#     async def _get_company_uuid(
#         self,
#         crm_company_id: str | None,
#         source_system_id: int,
#     ) -> uuid.UUID | None:
#         if not crm_company_id:
#             return None

#         cache_key = (crm_company_id, source_system_id)
#         if cache_key in self._company_cache:
#             return self._company_cache[cache_key]

#         result = await self.db.execute(
#             select(Company).where(
#                 Company.crm_company_id == crm_company_id,
#                 Company.source_system_id == source_system_id,
#             )
#         )
#         company = result.scalars().first()

#         internal_id = company.id if company else None
#         self._company_cache[cache_key] = internal_id

#         if not internal_id:
#             logger.warning(
#                 "Company crm_id=%r source=%d not found — "
#                 "company_id will be NULL. Sync companies first.",
#                 crm_company_id, source_system_id,
#             )
#         return internal_id

#     # ------------------------------------------------------------------
#     # Save single ticket
#     # ------------------------------------------------------------------
#     async def _save_ticket(
#         self,
#         ticket: NormalizedTicket,
#         source_system_id: int,
#         status_id: int,
#         priority_id: int | None,
#         agent_id: uuid.UUID | None,
#         customer_id: uuid.UUID | None,
#         company_id: uuid.UUID | None,
#     ) -> bool:
#         """Upsert one normalized ticket. Returns True if created, False if updated."""
#         data = {
#             "title": ticket.title,
#             "description": ticket.description,
#             "status_id": status_id,
#             "priority_id": priority_id,
#             "agent_id": agent_id,        # already uuid.UUID or None
#             "customer_id": customer_id,  # already uuid.UUID or None
#             "company_id": company_id,    # already uuid.UUID or None
#             "created_at": ticket.created_at,
#             "updated_at": ticket.updated_at,
#             "closed_at": ticket.closed_at,
#             "is_deleted": False,
#             "deleted_by_source": False,
#         }

#         _, created = await self.repo.upsert(
#             crm_ticket_id=ticket.crm_ticket_id,
#             source_system_id=source_system_id,
#             data=data,
#         )
#         return created

#     # ------------------------------------------------------------------
#     # Main sync entry point
#     # ------------------------------------------------------------------
#     async def sync_tickets(
#         self,
#         normalized_tickets: list[NormalizedTicket],
#         source_system: str,
#     ) -> SyncResult:
#         """
#         Resolve all FK IDs and upsert a list of NormalizedTickets into the DB.

#         Resolution order per ticket:
#           1. status_id     — from ticket_status table (required, falls back to open)
#           2. priority_id   — from ticket_priority table (optional, NULL if not found)
#           3. agent_id      — from agents table by (crm_agent_id, source_system_id)
#           4. customer_id   — from customers table by (crm_customer_id, source_system_id)
#           5. company_id    — from companies table by (crm_company_id, source_system_id)

#         Args:
#             normalized_tickets: List from ZammadService or EspoService.
#             source_system:      "zammad" or "espocrm".

#         Returns:
#             SyncResult with counts.
#         """
#         result = SyncResult(
#             source_system=source_system,
#             total_fetched=len(normalized_tickets),
#             created=0,
#             updated=0,
#             failed=0,
#         )

#         if not normalized_tickets:
#             logger.info("No tickets to sync for %s", source_system)
#             return result

#         source_system_id = await self._get_source_system_id(source_system)
#         if not source_system_id:
#             result.failed = len(normalized_tickets)
#             return result

#         logger.info(
#             "Syncing %d tickets from %s (source_system_id=%d)",
#             len(normalized_tickets), source_system, source_system_id,
#         )

#         for ticket in normalized_tickets:
#             try:
#                 # ---- required ----
#                 status_id = await self._get_status_id(ticket.status)
#                 if not status_id:
#                     logger.error(
#                         "Skipping ticket %s — could not resolve status '%s'",
#                         ticket.crm_ticket_id, ticket.status,
#                     )
#                     result.failed += 1
#                     continue

#                 # ---- optional FK resolutions ----
#                 priority_id  = await self._get_priority_id(ticket.priority)
#                 agent_id     = await self._get_agent_uuid(ticket.crm_agent_id, source_system_id)
#                 customer_id  = await self._get_customer_uuid(ticket.crm_customer_id, source_system_id)
#                 company_id   = await self._get_company_uuid(ticket.crm_company_id, source_system_id)

#                 created = await self._save_ticket(
#                     ticket=ticket,
#                     source_system_id=source_system_id,
#                     status_id=status_id,
#                     priority_id=priority_id,
#                     agent_id=agent_id,
#                     customer_id=customer_id,
#                     company_id=company_id,
#                 )

#                 if created:
#                     result.created += 1
#                 else:
#                     result.updated += 1

#             except Exception as exc:
#                 logger.error(
#                     "Failed to save ticket %s: %s",
#                     ticket.crm_ticket_id, exc,
#                 )
#                 result.failed += 1

#         logger.info(
#             "Sync complete for %s — created: %d, updated: %d, failed: %d",
#             source_system, result.created, result.updated, result.failed,
#         )
#         return result

"""
app/services/sync_service.py

Sync service — saves NormalizedTickets into the database.

CRM ID → Internal UUID resolution:
  Every CRM stores its entities with its own ID format:
    Zammad   → integer IDs
    EspoCRM  → string UUIDs

  Raw CRM IDs are resolved via:
    (crm_*_id, source_system_id) → internal UUID

  If an agent / customer / company is not found,
  the field is set to NULL and syncing continues.
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
    total_fetched: int
    created: int
    updated: int
    failed: int


class SyncService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = TicketRepository(db)

        # In-memory caches (per sync run)
        self._status_cache: dict[str, int] = {}
        self._priority_cache: dict[str, int] = {}
        self._agent_cache: dict[tuple[str, int], uuid.UUID | None] = {}
        self._customer_cache: dict[tuple[str, int], uuid.UUID | None] = {}
        self._company_cache: dict[tuple[str, int], uuid.UUID | None] = {}

    # ------------------------------------------------------------------
    # Source system
    # ------------------------------------------------------------------
    async def _get_source_system_id(self, name: str) -> int | None:
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.system_name == name.lower())
        )
        source = result.scalars().first()
        if not source:
            logger.error("Source system '%s' not found", name)
        return source.id if source else None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def _get_status_id(self, name: str) -> int | None:
        if name in self._status_cache:
            return self._status_cache[name]

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
        return None

    # ------------------------------------------------------------------
    # Priority
    # ------------------------------------------------------------------
    async def _get_priority_id(self, name: str | None) -> int | None:
        if not name:
            return None
        if name in self._priority_cache:
            return self._priority_cache[name]

        result = await self.db.execute(
            select(TicketPriority).where(
                TicketPriority.priority_name == name.lower()
            )
        )
        priority = result.scalars().first()

        if priority:
            self._priority_cache[name] = priority.id
            return priority.id
        return None

    # ------------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------------
    async def _get_agent_uuid(
        self,
        crm_agent_id: str | None,
        source_system_id: int,
    ) -> uuid.UUID | None:
        if not crm_agent_id:
            return None

        key = (crm_agent_id, source_system_id)
        if key in self._agent_cache:
            return self._agent_cache[key]

        result = await self.db.execute(
            select(Agent).where(
                Agent.crm_agent_id == crm_agent_id,
                Agent.source_system_id == source_system_id,
            )
        )
        agent = result.scalars().first()
        value = agent.id if agent else None
        self._agent_cache[key] = value

        if not value:
            logger.warning("Agent crm_id=%r not found", crm_agent_id)
        return value

    # ------------------------------------------------------------------
    # Customer
    # ------------------------------------------------------------------
    async def _get_customer_uuid(
        self,
        crm_customer_id: str | None,
        source_system_id: int,
    ) -> uuid.UUID | None:
        if not crm_customer_id:
            return None

        key = (crm_customer_id, source_system_id)
        if key in self._customer_cache:
            return self._customer_cache[key]

        result = await self.db.execute(
            select(Customer).where(
                Customer.crm_customer_id == crm_customer_id,
                Customer.source_system_id == source_system_id,
            )
        )
        customer = result.scalars().first()
        value = customer.id if customer else None
        self._customer_cache[key] = value

        if not value:
            logger.warning("Customer crm_id=%r not found", crm_customer_id)
        return value

    # ------------------------------------------------------------------
    # Company
    # ------------------------------------------------------------------
    async def _get_company_uuid(
        self,
        crm_company_id: str | None,
        source_system_id: int,
    ) -> uuid.UUID | None:
        if not crm_company_id:
            return None

        key = (crm_company_id, source_system_id)
        if key in self._company_cache:
            return self._company_cache[key]

        result = await self.db.execute(
            select(Company).where(
                Company.crm_company_id == crm_company_id,
                Company.source_system_id == source_system_id,
            )
        )
        company = result.scalars().first()
        value = company.id if company else None
        self._company_cache[key] = value

        if not value:
            logger.warning("Company crm_id=%r not found", crm_company_id)
        return value

    # ------------------------------------------------------------------
    # Save ticket
    # ------------------------------------------------------------------
    async def _save_ticket(
        self,
        ticket: NormalizedTicket,
        source_system_id: int,
        status_id: int,
        priority_id: int | None,
        agent_id: uuid.UUID | None,
        customer_id: uuid.UUID | None,
        company_id: uuid.UUID | None,
    ) -> bool:
        data = {
            "tenant_id": None,  # explicitly NULL (for now)
            "title": ticket.title,
            "description": ticket.description,
            "status_id": status_id,
            "priority_id": priority_id,
            "agent_id": agent_id,
            "customer_id": customer_id,
            "company_id": company_id,
            "created_at": ticket.created_at,
            "updated_at": ticket.updated_at,
            "closed_at": ticket.closed_at,
            "is_deleted": False,
            "deleted_by_source": False,
        }

        _, created = await self.repo.upsert(
            crm_ticket_id=ticket.crm_ticket_id,
            source_system_id=source_system_id,
            data=data,
        )
        return created

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def sync_tickets(
        self,
        normalized_tickets: list[NormalizedTicket],
        source_system: str,
    ) -> SyncResult:
        result = SyncResult(
            source_system=source_system,
            total_fetched=len(normalized_tickets),
            created=0,
            updated=0,
            failed=0,
        )

        if not normalized_tickets:
            return result

        source_system_id = await self._get_source_system_id(source_system)
        if not source_system_id:
            result.failed = len(normalized_tickets)
            return result

        for ticket in normalized_tickets:
            try:
                status_id = await self._get_status_id(ticket.status)
                if not status_id:
                    result.failed += 1
                    continue

                created = await self._save_ticket(
                    ticket=ticket,
                    source_system_id=source_system_id,
                    status_id=status_id,
                    priority_id=await self._get_priority_id(ticket.priority),
                    agent_id=await self._get_agent_uuid(ticket.crm_agent_id, source_system_id),
                    customer_id=await self._get_customer_uuid(ticket.crm_customer_id, source_system_id),
                    company_id=await self._get_company_uuid(ticket.crm_company_id, source_system_id),
                )

                result.created += int(created)
                result.updated += int(not created)

            except Exception as exc:
                logger.error("Failed to save ticket %s: %s", ticket.crm_ticket_id, exc)
                result.failed += 1

        return result
