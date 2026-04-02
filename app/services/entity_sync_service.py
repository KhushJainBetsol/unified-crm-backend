# """
# app/services/entity_sync_service.py

# Syncs agents, customers, and companies from CRM systems into the DB.

# Must be run BEFORE ticket sync so that ticket sync can resolve:
#   crm_agent_id    → agents.id    (UUID)
#   crm_customer_id → customers.id (UUID)
#   crm_company_id  → companies.id (UUID)

# Field mappings:
#   Zammad agent     → id, firstname+lastname, email
#   Zammad customer  → id, firstname+lastname, email
#   Zammad org       → id, name
#   EspoCRM user     → id, firstName+lastName, emailAddress
#   EspoCRM contact  → id, firstName+lastName, emailAddress
#   EspoCRM account  → id, name
# """

# from __future__ import annotations

# import logging
# from dataclasses import dataclass, field

# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.models.agent import Agent
# from app.models.company import Company
# from app.models.customer import Customer

# logger = logging.getLogger(__name__)


# @dataclass
# class EntitySyncResult:
#     source_system: str
#     agents_created: int = 0
#     agents_updated: int = 0
#     customers_created: int = 0
#     customers_updated: int = 0
#     companies_created: int = 0
#     companies_updated: int = 0
#     failed: int = 0


# class EntitySyncService:

#     def __init__(self, db: AsyncSession, source_system_id: int) -> None:
#         self.db = db
#         self.source_system_id = source_system_id

#     # ------------------------------------------------------------------
#     # Internal upsert helpers
#     # ------------------------------------------------------------------
#     async def _upsert_agent(self, crm_agent_id: str, name: str, email: str | None) -> bool:
#         """Returns True if created, False if updated."""
#         result = await self.db.execute(
#             select(Agent).where(
#                 Agent.crm_agent_id == crm_agent_id,
#                 Agent.source_system_id == self.source_system_id,
#             )
#         )
#         agent = result.scalars().first()

#         if agent:
#             agent.name = name
#             agent.email = email
#             await self.db.flush()
#             return False
#         else:
#             self.db.add(Agent(
#                 crm_agent_id=crm_agent_id,
#                 source_system_id=self.source_system_id,
#                 name=name,
#                 email=email,
#                 is_active=True,
#             ))
#             await self.db.flush()
#             return True

#     async def _upsert_customer(
#         self,
#         crm_customer_id: str,
#         first_name: str,
#         last_name: str | None,
#         email: str | None,
#         phone: str | None = None,
#     ) -> bool:
#         """Returns True if created, False if updated."""
#         result = await self.db.execute(
#             select(Customer).where(
#                 Customer.crm_customer_id == crm_customer_id,
#                 Customer.source_system_id == self.source_system_id,
#             )
#         )
#         customer = result.scalars().first()

#         if customer:
#             customer.first_name = first_name
#             customer.last_name = last_name
#             customer.email = email
#             customer.phone = phone
#             await self.db.flush()
#             return False
#         else:
#             self.db.add(Customer(
#                 crm_customer_id=crm_customer_id,
#                 source_system_id=self.source_system_id,
#                 first_name=first_name,
#                 last_name=last_name,
#                 email=email,
#                 phone=phone,
#             ))
#             await self.db.flush()
#             return True

#     async def _upsert_company(
#         self,
#         crm_company_id: str,
#         company_name: str,
#         email: str | None = None,
#         phone: str | None = None,
#     ) -> bool:
#         """Returns True if created, False if updated."""
#         result = await self.db.execute(
#             select(Company).where(
#                 Company.crm_company_id == crm_company_id,
#                 Company.source_system_id == self.source_system_id,
#             )
#         )
#         company = result.scalars().first()

#         if company:
#             company.company_name = company_name
#             company.email = email
#             company.phone = phone
#             await self.db.flush()
#             return False
#         else:
#             self.db.add(Company(
#                 crm_company_id=crm_company_id,
#                 source_system_id=self.source_system_id,
#                 company_name=company_name,
#                 email=email,
#                 phone=phone,
#             ))
#             await self.db.flush()
#             return True

#     # ------------------------------------------------------------------
#     # Zammad sync methods
#     # ------------------------------------------------------------------
#     async def sync_zammad_agents(self, raw_users: list[dict]) -> tuple[int, int]:
#         """Sync Zammad users (agents) → agents table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_users:
#             try:
#                 crm_agent_id = str(raw["id"])
#                 first = raw.get("firstname") or ""
#                 last  = raw.get("lastname") or ""
#                 name  = f"{first} {last}".strip() or f"Agent {crm_agent_id}"
#                 email = raw.get("email") or None

#                 was_created = await self._upsert_agent(crm_agent_id, name, email)
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync Zammad agent id=%r: %s", raw.get("id"), exc)
#         return created, updated

#     async def sync_zammad_customers(self, raw_customers: list[dict]) -> tuple[int, int]:
#         """Sync Zammad customers → customers table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_customers:
#             try:
#                 crm_customer_id = str(raw["id"])
#                 first_name = raw.get("firstname") or f"Customer"
#                 last_name  = raw.get("lastname") or None
#                 email      = raw.get("email") or None
#                 phone      = raw.get("phone") or None

#                 was_created = await self._upsert_customer(
#                     crm_customer_id, first_name, last_name, email, phone
#                 )
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync Zammad customer id=%r: %s", raw.get("id"), exc)
#         return created, updated

#     async def sync_zammad_companies(self, raw_orgs: list[dict]) -> tuple[int, int]:
#         """Sync Zammad organizations → companies table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_orgs:
#             try:
#                 crm_company_id = str(raw["id"])
#                 company_name   = raw.get("name") or f"Organization {crm_company_id}"

#                 was_created = await self._upsert_company(crm_company_id, company_name)
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync Zammad org id=%r: %s", raw.get("id"), exc)
#         return created, updated

#     # ------------------------------------------------------------------
#     # EspoCRM sync methods
#     # ------------------------------------------------------------------
#     async def sync_espo_agents(self, raw_users: list[dict]) -> tuple[int, int]:
#         """Sync EspoCRM users → agents table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_users:
#             try:
#                 crm_agent_id = str(raw["id"])
#                 first = raw.get("firstName") or ""
#                 last  = raw.get("lastName") or ""
#                 name  = f"{first} {last}".strip() or raw.get("userName") or f"Agent {crm_agent_id}"
#                 email = raw.get("emailAddress") or None

#                 was_created = await self._upsert_agent(crm_agent_id, name, email)
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync EspoCRM user id=%r: %s", raw.get("id"), exc)
#         return created, updated

#     async def sync_espo_customers(self, raw_contacts: list[dict]) -> tuple[int, int]:
#         """Sync EspoCRM contacts → customers table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_contacts:
#             try:
#                 crm_customer_id = str(raw["id"])
#                 first_name = raw.get("firstName") or "Contact"
#                 last_name  = raw.get("lastName") or None
#                 email      = raw.get("emailAddress") or None
#                 phone      = raw.get("phoneNumber") or None

#                 was_created = await self._upsert_customer(
#                     crm_customer_id, first_name, last_name, email, phone
#                 )
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync EspoCRM contact id=%r: %s", raw.get("id"), exc)
#         return created, updated

#     async def sync_espo_companies(self, raw_accounts: list[dict]) -> tuple[int, int]:
#         """Sync EspoCRM accounts → companies table. Returns (created, updated)."""
#         created = updated = 0
#         for raw in raw_accounts:
#             try:
#                 crm_company_id = str(raw["id"])
#                 company_name   = raw.get("name") or f"Account {crm_company_id}"
#                 phone          = raw.get("phoneNumber") or None
#                 email          = raw.get("emailAddress") or None

#                 was_created = await self._upsert_company(crm_company_id, company_name, email, phone)
#                 if was_created:
#                     created += 1
#                 else:
#                     updated += 1
#             except Exception as exc:
#                 logger.error("Failed to sync EspoCRM account id=%r: %s", raw.get("id"), exc)
#         return created, updated
"""
app/services/entity_sync_service.py

Syncs agents, customers, and companies from CRM systems into the DB.

Must be run BEFORE ticket sync so that ticket sync can resolve:
  crm_agent_id    → agents.id    (UUID)
  crm_customer_id → customers.id (UUID)
  crm_company_id  → companies.id (UUID)

Field mappings:
  Zammad agent     → id, firstname+lastname, email
  Zammad customer  → id, firstname+lastname, email
  Zammad org       → id, name
  EspoCRM user     → id, firstName+lastName, emailAddress
  EspoCRM contact  → id, firstName+lastName, emailAddress
  EspoCRM account  → id, name
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.company import Company
from app.models.customer import Customer

logger = logging.getLogger(__name__)


@dataclass
class EntitySyncResult:
    source_system: str
    agents_created: int = 0
    agents_updated: int = 0
    customers_created: int = 0
    customers_updated: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    failed: int = 0


class EntitySyncService:

    def __init__(self, db: AsyncSession, source_system_id: int) -> None:
        self.db = db
        self.source_system_id = source_system_id

    # ------------------------------------------------------------------
    # Internal upsert helpers
    # ------------------------------------------------------------------

    async def _upsert_agent(
        self,
        crm_agent_id: str,
        name: str,
        email: str | None,
    ) -> bool:
        """Returns True if created, False if updated."""
        result = await self.db.execute(
            select(Agent).where(
                Agent.crm_agent_id == crm_agent_id,
                Agent.source_system_id == self.source_system_id,
            )
        )
        agent = result.scalars().first()

        if agent:
            agent.name = name
            agent.email = email
            await self.db.flush()
            return False

        self.db.add(Agent(
            crm_agent_id=crm_agent_id,
            source_system_id=self.source_system_id,
            name=name,
            email=email,
            is_active=True,
        ))
        await self.db.flush()
        return True

    async def _upsert_customer(
        self,
        crm_customer_id: str,
        first_name: str,
        last_name: str | None,
        email: str | None,
        phone: str | None = None,
    ) -> bool:
        """Returns True if created, False if updated."""
        result = await self.db.execute(
            select(Customer).where(
                Customer.crm_customer_id == crm_customer_id,
                Customer.source_system_id == self.source_system_id,
            )
        )
        customer = result.scalars().first()

        if customer:
            customer.first_name = first_name
            customer.last_name = last_name
            customer.email = email
            customer.phone = phone
            await self.db.flush()
            return False

        self.db.add(Customer(
            crm_customer_id=crm_customer_id,
            source_system_id=self.source_system_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
        ))
        await self.db.flush()
        return True

    async def _upsert_company(
        self,
        crm_company_id: str,
        company_name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> bool:
        """Returns True if created, False if updated."""
        result = await self.db.execute(
            select(Company).where(
                Company.crm_company_id == crm_company_id,
                Company.source_system_id == self.source_system_id,
            )
        )
        company = result.scalars().first()

        if company:
            company.company_name = company_name
            company.email = email
            company.phone = phone
            await self.db.flush()
            return False

        self.db.add(Company(
            tenant_id=None,  # explicitly NULL
            crm_company_id=crm_company_id,
            source_system_id=self.source_system_id,
            company_name=company_name,
            email=email,
            phone=phone,
        ))
        await self.db.flush()
        return True

    # ------------------------------------------------------------------
    # Zammad sync methods
    # ------------------------------------------------------------------

    async def sync_zammad_agents(self, raw_users: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_users:
            try:
                crm_agent_id = str(raw["id"])
                first = raw.get("firstname") or ""
                last = raw.get("lastname") or ""
                name = f"{first} {last}".strip() or f"Agent {crm_agent_id}"
                email = raw.get("email") or None

                if await self._upsert_agent(crm_agent_id, name, email):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync Zammad agent id=%r: %s", raw.get("id"), exc)

        return created, updated

    async def sync_zammad_customers(self, raw_customers: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_customers:
            try:
                crm_customer_id = str(raw["id"])
                first_name = raw.get("firstname") or "Customer"
                last_name = raw.get("lastname") or None
                email = raw.get("email") or None
                phone = raw.get("phone") or None

                if await self._upsert_customer(
                    crm_customer_id, first_name, last_name, email, phone
                ):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync Zammad customer id=%r: %s", raw.get("id"), exc)

        return created, updated

    async def sync_zammad_companies(self, raw_orgs: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_orgs:
            try:
                crm_company_id = str(raw["id"])
                company_name = raw.get("name") or f"Organization {crm_company_id}"

                if await self._upsert_company(crm_company_id, company_name):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync Zammad org id=%r: %s", raw.get("id"), exc)

        return created, updated

    # ------------------------------------------------------------------
    # EspoCRM sync methods
    # ------------------------------------------------------------------

    async def sync_espo_agents(self, raw_users: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_users:
            try:
                crm_agent_id = str(raw["id"])
                first = raw.get("firstName") or ""
                last = raw.get("lastName") or ""
                name = f"{first} {last}".strip() or raw.get("userName") or f"Agent {crm_agent_id}"
                email = raw.get("emailAddress") or None

                if await self._upsert_agent(crm_agent_id, name, email):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync EspoCRM user id=%r: %s", raw.get("id"), exc)

        return created, updated

    async def sync_espo_customers(self, raw_contacts: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_contacts:
            try:
                crm_customer_id = str(raw["id"])
                first_name = raw.get("firstName") or "Contact"
                last_name = raw.get("lastName") or None
                email = raw.get("emailAddress") or None
                phone = raw.get("phoneNumber") or None

                if await self._upsert_customer(
                    crm_customer_id, first_name, last_name, email, phone
                ):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync EspoCRM contact id=%r: %s", raw.get("id"), exc)

        return created, updated

    async def sync_espo_companies(self, raw_accounts: list[dict]) -> tuple[int, int]:
        created = updated = 0
        for raw in raw_accounts:
            try:
                crm_company_id = str(raw["id"])
                company_name = raw.get("name") or f"Account {crm_company_id}"
                phone = raw.get("phoneNumber") or None
                email = raw.get("emailAddress") or None

                if await self._upsert_company(crm_company_id, company_name, email, phone):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                logger.error("Failed to sync EspoCRM account id=%r: %s", raw.get("id"), exc)

        return created, updated