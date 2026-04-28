# """
# app/services/scheduler.py

# Multitenant CRM sync scheduler.

# Key change vs previous version:
#   _sync_espo_entities_for_tenant no longer passes raw_customers to
#   svc.sync_espo_agents().  The email-based user resolution in
#   EspoClient.get_agents_by_account() now returns full User dicts
#   directly, so no separate contact enrichment step is needed.
# """

# from __future__ import annotations

# import logging
# import uuid

# from apscheduler.schedulers.asyncio import AsyncIOScheduler
# from apscheduler.triggers.interval import IntervalTrigger
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.core.database import async_session_maker
# from app.core.settings import get_settings
# from app.integrations.espo.client import EspoAuthError, EspoClient, EspoClientError
# from app.integrations.espo.service import EspoService
# from app.integrations.zammad.client import ZammadAuthError, ZammadClient, ZammadClientError
# from app.integrations.zammad.service import ZammadService
# from app.models.source_system import SourceSystem
# from app.models.tenant import Tenant
# from app.models.tenant_source_systems import TenantSourceSystem
# from app.services.entity_sync_service import EntitySyncService
# from app.services.sync_service import SyncService

# logger = logging.getLogger(__name__)
# scheduler = AsyncIOScheduler()


# # ---------------------------------------------------------------------------
# # DB helpers
# # ---------------------------------------------------------------------------

# async def _get_all_active_tenants() -> list[Tenant]:
#     async with async_session_maker() as db:
#         result = await db.execute(
#             select(Tenant).where(Tenant.is_active == True)  # noqa: E712
#         )
#         return list(result.scalars().all())


# async def _get_tenant_source_systems(
#     tenant_id: uuid.UUID,
# ) -> list[TenantSourceSystem]:
#     async with async_session_maker() as db:
#         result = await db.execute(
#             select(TenantSourceSystem).where(
#                 TenantSourceSystem.tenant_id  == tenant_id,
#                 TenantSourceSystem.is_active  == True,   # noqa: E712
#                 TenantSourceSystem.crm_org_id != None,   # noqa: E711
#             )
#         )
#         return list(result.scalars().all())


# async def _get_source_system_name(source_system_id: int) -> str | None:
#     async with async_session_maker() as db:
#         result = await db.execute(
#             select(SourceSystem).where(SourceSystem.id == source_system_id)
#         )
#         source = result.scalars().first()
#         if not source:
#             logger.error("Source system id=%d not found in DB.", source_system_id)
#             return None
#         return source.system_name


# # ---------------------------------------------------------------------------
# # Per-tenant per-source-system sync helpers
# # ---------------------------------------------------------------------------

# async def _sync_zammad_entities_for_tenant(
#     tenant_id: uuid.UUID,
#     source_system_id: int,
#     crm_org_id: str,
# ) -> tuple[int, int, int, int, int, int]:
#     async with ZammadClient() as client:
#         raw_agents    = await client.get_agents_by_org(crm_org_id)
#         raw_customers = await client.get_customers_by_org(crm_org_id)
#         raw_org       = await client.get_organization_by_id(crm_org_id)

#     async with async_session_maker() as db:
#         try:
#             svc = EntitySyncService(db, source_system_id, tenant_id)
#             agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
#             customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
#             companies_c, companies_u = await svc.sync_zammad_companies([raw_org])
#             await db.commit()
#             return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
#         except Exception:
#             await db.rollback()
#             raise


# async def _sync_zammad_tickets_for_tenant(
#     tenant_id: uuid.UUID,
#     source_system_id: int,
#     crm_org_id: str,
# ) -> object:
#     async with ZammadClient() as client:
#         raw_tickets = await client.get_tickets_by_org(crm_org_id)
#         normalized  = ZammadService(client).normalize_raw_tickets(raw_tickets)

#     async with async_session_maker() as db:
#         try:
#             result = await SyncService(db).sync_tickets(
#                 normalized_tickets = normalized,
#                 source_system      = "zammad",
#                 tenant_id          = tenant_id,
#             )
#             await db.commit()
#             return result
#         except Exception:
#             await db.rollback()
#             raise


# async def _sync_espo_entities_for_tenant(
#     tenant_id: uuid.UUID,
#     source_system_id: int,
#     crm_org_id: str,
# ) -> tuple[int, int, int, int, int, int]:
#     """
#     Fetch and upsert EspoCRM agents, contacts (customers), and the single
#     Account that corresponds to crm_org_id — all scoped to tenant_id.

#     get_agents_by_account() now resolves Users via email:
#       Contact.emailAddress → GET /api/v1/User?emailAddress=<email>
#     so raw_agents are already full User dicts with correct IDs and all
#     fields. No contact enrichment pass is needed.

#     Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
#     """
#     async with EspoClient() as client:
#         raw_agents    = await client.get_agents_by_account(crm_org_id)
#         raw_customers = await client.get_contacts_by_account(crm_org_id)
#         raw_account   = await client.get_account_by_id(crm_org_id)

#     async with async_session_maker() as db:
#         try:
#             svc = EntitySyncService(db, source_system_id, tenant_id)
#             agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
#             customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
#             companies_c, companies_u = await svc.sync_espo_companies([raw_account])
#             await db.commit()
#             return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
#         except Exception:
#             await db.rollback()
#             raise


# async def _sync_espo_tickets_for_tenant(
#     tenant_id: uuid.UUID,
#     source_system_id: int,
#     crm_org_id: str,
# ) -> object:
#     async with EspoClient() as client:
#         raw_tickets = await client.get_tickets_by_account(crm_org_id)
#         normalized  = EspoService(client).normalize_raw_tickets(raw_tickets)

#     async with async_session_maker() as db:
#         try:
#             result = await SyncService(db).sync_tickets(
#                 normalized_tickets = normalized,
#                 source_system      = "espocrm",
#                 tenant_id          = tenant_id,
#             )
#             await db.commit()
#             return result
#         except Exception:
#             await db.rollback()
#             raise


# # ---------------------------------------------------------------------------
# # Per-(tenant, source_system) orchestration
# # ---------------------------------------------------------------------------

# async def _sync_one_tenant_source_system(
#     tenant_id: uuid.UUID,
#     tss: TenantSourceSystem,
# ) -> dict:
#     source_system_name = await _get_source_system_name(tss.source_system_id)
#     if not source_system_name:
#         return {"error": f"Unknown source_system_id={tss.source_system_id}"}

#     crm_org_id = tss.crm_org_id

#     logger.info(
#         "Starting sync — tenant=%s source=%s crm_org_id=%s",
#         tenant_id, source_system_name, crm_org_id,
#     )

#     try:
#         if source_system_name == "zammad":
#             (
#                 agents_c, agents_u,
#                 customers_c, customers_u,
#                 companies_c, companies_u,
#             ) = await _sync_zammad_entities_for_tenant(
#                 tenant_id, tss.source_system_id, crm_org_id
#             )
#             ticket_result = await _sync_zammad_tickets_for_tenant(
#                 tenant_id, tss.source_system_id, crm_org_id
#             )

#         elif source_system_name == "espocrm":
#             (
#                 agents_c, agents_u,
#                 customers_c, customers_u,
#                 companies_c, companies_u,
#             ) = await _sync_espo_entities_for_tenant(
#                 tenant_id, tss.source_system_id, crm_org_id
#             )
#             ticket_result = await _sync_espo_tickets_for_tenant(
#                 tenant_id, tss.source_system_id, crm_org_id
#             )

#         else:
#             logger.warning(
#                 "No sync handler for source_system='%s' — skipping tenant=%s",
#                 source_system_name, tenant_id,
#             )
#             return {"skipped": True, "reason": f"No handler for {source_system_name}"}

#         logger.info(
#             "Sync done — tenant=%s source=%s crm_org=%s | "
#             "agents(%d/%d) customers(%d/%d) companies(%d/%d) "
#             "tickets: fetched=%d created=%d updated=%d failed=%d",
#             tenant_id, source_system_name, crm_org_id,
#             agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
#             ticket_result.total_fetched, ticket_result.created,
#             ticket_result.updated, ticket_result.failed,
#         )

#         return _build_result(
#             agents_c, agents_u, customers_c, customers_u,
#             companies_c, companies_u, ticket_result,
#         )

#     except (ZammadClientError, ZammadAuthError, EspoClientError, EspoAuthError) as exc:
#         logger.error(
#             "CRM error for tenant=%s source=%s crm_org=%s: %s",
#             tenant_id, source_system_name, crm_org_id, exc,
#         )
#         return {"error": str(exc)}
#     except Exception as exc:
#         logger.exception(
#             "Unexpected error for tenant=%s source=%s crm_org=%s: %s",
#             tenant_id, source_system_name, crm_org_id, exc,
#         )
#         return {"error": str(exc)}


# # ---------------------------------------------------------------------------
# # Main sync entry points
# # ---------------------------------------------------------------------------

# async def run_all_tenants_full_sync() -> None:
#     tenants = await _get_all_active_tenants()
#     logger.info("Scheduler: starting full sync for %d active tenants", len(tenants))
#     for tenant in tenants:
#         tss_rows = await _get_tenant_source_systems(tenant.id)
#         if not tss_rows:
#             logger.debug(
#                 "Tenant %s has no active source systems with crm_org_id — skipping",
#                 tenant.id,
#             )
#             continue
#         for tss in tss_rows:
#             await _sync_one_tenant_source_system(tenant.id, tss)
#     logger.info("Scheduler: full multitenant sync complete")


# async def run_tenant_full_sync(
#     tenant_id: uuid.UUID,
#     db: AsyncSession | None = None,
# ) -> dict:
#     tss_rows = await _get_tenant_source_systems(tenant_id)
#     if not tss_rows:
#         logger.warning("Tenant %s has no active source systems with crm_org_id", tenant_id)
#         return {}
#     all_results: dict[str, dict] = {}
#     for tss in tss_rows:
#         source_system_name = await _get_source_system_name(tss.source_system_id)
#         key = f"{source_system_name}:{tss.crm_org_id}"
#         all_results[key] = await _sync_one_tenant_source_system(tenant_id, tss)
#     return all_results


# async def run_zammad_full_sync(db: AsyncSession | None = None) -> dict:
#     tenants = await _get_all_active_tenants()
#     all_results: dict = {}
#     for tenant in tenants:
#         tss_rows = await _get_tenant_source_systems(tenant.id)
#         for tss in tss_rows:
#             name = await _get_source_system_name(tss.source_system_id)
#             if name != "zammad":
#                 continue
#             key = f"tenant:{tenant.id}:zammad:{tss.crm_org_id}"
#             all_results[key] = await _sync_one_tenant_source_system(tenant.id, tss)
#     return all_results


# async def run_espocrm_full_sync(db: AsyncSession | None = None) -> dict:
#     tenants = await _get_all_active_tenants()
#     all_results: dict = {}
#     for tenant in tenants:
#         tss_rows = await _get_tenant_source_systems(tenant.id)
#         for tss in tss_rows:
#             name = await _get_source_system_name(tss.source_system_id)
#             if name != "espocrm":
#                 continue
#             key = f"tenant:{tenant.id}:espocrm:{tss.crm_org_id}"
#             all_results[key] = await _sync_one_tenant_source_system(tenant.id, tss)
#     return all_results


# # ---------------------------------------------------------------------------
# # Private helper
# # ---------------------------------------------------------------------------

# def _build_result(
#     agents_c, agents_u,
#     customers_c, customers_u,
#     companies_c, companies_u,
#     ticket_result,
# ) -> dict:
#     return {
#         "entities": {
#             "agents":    {"created": agents_c,    "updated": agents_u},
#             "customers": {"created": customers_c, "updated": customers_u},
#             "companies": {"created": companies_c, "updated": companies_u},
#         },
#         "tickets": {
#             "total_fetched": ticket_result.total_fetched,
#             "created":       ticket_result.created,
#             "updated":       ticket_result.updated,
#             "failed":        ticket_result.failed,
#         },
#     }


# # ---------------------------------------------------------------------------
# # Scheduler setup
# # ---------------------------------------------------------------------------

# def start_scheduler() -> None:
#     scheduler.add_job(
#         run_all_tenants_full_sync,
#         trigger=IntervalTrigger(minutes=get_settings().SYNC_INTERVAL_MINUTES),
#         id="full_crm_sync",
#         replace_existing=True,
#         misfire_grace_time=60,
#     )
#     scheduler.start()
#     logger.info(
#         "CRM sync scheduler started — interval=%d min",
#         get_settings().SYNC_INTERVAL_MINUTES,
#     )


# def stop_scheduler() -> None:
#     scheduler.shutdown(wait=False)
#     logger.info("CRM sync scheduler stopped.")

"""
app/services/scheduler.py

Multitenant CRM sync scheduler — ADAPTER PATTERN (fully migrated).

All direct EspoClient / ZammadClient / EspoService / ZammadService calls
have been removed.  Every sync now goes through the CrmAdapterFactory,
which resolves credentials from Infisical/DB and returns a fully configured
BaseCrmAdapter.  The scheduler is 100% CRM-agnostic — adding a third CRM
requires zero changes here.

Key changes vs the legacy version
-----------------------------------
  REMOVED:  EspoClient, EspoService, ZammadClient, ZammadService imports
  REMOVED:  _sync_zammad_entities_for_tenant  (direct client call)
  REMOVED:  _sync_zammad_tickets_for_tenant   (direct client call)
  REMOVED:  _sync_espo_entities_for_tenant    (direct client call)
  REMOVED:  _sync_espo_tickets_for_tenant     (direct client call)
  REMOVED:  if/elif source_system_name == "zammad" / "espocrm" branch

  ADDED:    _sync_entities_via_adapter        (adapter pattern)
  ADDED:    _sync_tickets_via_adapter         (adapter pattern)
  ADDED:    _get_factory()                    (pulls CrmAdapterFactory from app.state)
  CHANGED:  _sync_one_tenant_source_system    (delegates to adapter helpers)
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base.client import CrmClientError
from app.core.database import async_session_maker
from app.core.settings import get_settings
from app.domain.models import UnifiedTicket
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory
from app.integrations.normalizer.schema import NormalizedTicket
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_source_systems import TenantSourceSystem
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# ---------------------------------------------------------------------------
# App-state accessor
# ---------------------------------------------------------------------------

_app_ref: Optional[FastAPI] = None


def set_app(app: FastAPI) -> None:
    """
    Store a reference to the FastAPI app so the scheduler can reach
    app.state.adapter_factory.  Call this once in main.py lifespan
    BEFORE start_scheduler():

        set_app(app)
        start_scheduler()
    """
    global _app_ref
    _app_ref = app


def _get_factory() -> CrmAdapterFactory:
    """Return the CrmAdapterFactory from app.state.  Raises RuntimeError if not set."""
    if _app_ref is None:
        raise RuntimeError(
            "Scheduler app reference is not set. "
            "Call set_app(app) in lifespan before start_scheduler()."
        )
    factory: Optional[CrmAdapterFactory] = getattr(
        _app_ref.state, "adapter_factory", None
    )
    if factory is None:
        raise RuntimeError(
            "CrmAdapterFactory is not initialised on app.state. "
            "Ensure _bootstrap_adapter_factory() completed successfully."
        )
    return factory


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_all_active_tenants() -> list[Tenant]:
    async with async_session_maker() as db:
        result = await db.execute(
            select(Tenant).where(Tenant.is_active == True)  # noqa: E712
        )
        return list(result.scalars().all())


async def _get_tenant_source_systems(
    tenant_id: uuid.UUID,
) -> list[TenantSourceSystem]:
    async with async_session_maker() as db:
        result = await db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.tenant_id  == tenant_id,
                TenantSourceSystem.is_active  == True,   # noqa: E712
                TenantSourceSystem.crm_org_id != None,   # noqa: E711
            )
        )
        return list(result.scalars().all())


async def _get_source_system_name(source_system_id: int) -> str | None:
    async with async_session_maker() as db:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.id == source_system_id)
        )
        source = result.scalars().first()
        if not source:
            logger.error("Source system id=%d not found in DB.", source_system_id)
            return None
        return source.system_name


# ---------------------------------------------------------------------------
# Adapter-pattern sync helpers  (CRM-agnostic)
# ---------------------------------------------------------------------------

def _unified_to_normalized(ticket: UnifiedTicket, source_system: str) -> NormalizedTicket:
    """Convert an adapter UnifiedTicket → NormalizedTicket for SyncService."""
    status_val = ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
    priority_val = ticket.priority.value if hasattr(ticket.priority, "value") else str(ticket.priority)
    if priority_val == "unknown":
        priority_val = None

    return NormalizedTicket(
        crm_ticket_id   = ticket.id,
        source_system   = source_system,
        title           = ticket.title or "No Title",
        description     = ticket.description,
        status          = status_val,
        priority        = priority_val,
        crm_agent_id    = ticket.assignee_id,
        crm_customer_id = ticket.customer_id,
        crm_company_id  = ticket.organization_id,
        created_at      = ticket.created_at,
        updated_at      = ticket.updated_at,
        closed_at       = None,
    )


async def _sync_entities_via_adapter(
    tenant_id: uuid.UUID,
    source_system_name: str,
    tss: TenantSourceSystem,
    factory: CrmAdapterFactory,
) -> tuple[int, int, int, int, int, int]:
    """
    Fetch agents, customers, and organizations from the CRM via the adapter
    and upsert them into the DB.

    Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
    """
    crm_org_id = tss.crm_org_id

    adapter = await factory.create(str(tss.integration_id))
    async with adapter:
        agents_result    = await adapter.fetch_agents(crm_org_id)
        customers_result = await adapter.fetch_customers(crm_org_id)
        orgs_result      = await adapter.fetch_organizations()

    async with async_session_maker() as db:
        try:
            svc = EntitySyncService(db, tss.source_system_id, tenant_id)
            agents_c,    agents_u    = await svc.sync_agents(agents_result.items,    source_system_name)
            customers_c, customers_u = await svc.sync_customers(customers_result.items, source_system_name)
            companies_c, companies_u = await svc.sync_companies(orgs_result.items,   source_system_name)
            await db.commit()
            return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
        except Exception:
            await db.rollback()
            raise


async def _sync_tickets_via_adapter(
    tenant_id: uuid.UUID,
    source_system_name: str,
    tss: TenantSourceSystem,
    factory: CrmAdapterFactory,
) -> object:
    """
    Fetch tickets from the CRM via the adapter, convert UnifiedTicket →
    NormalizedTicket, and upsert into the DB.
    """
    adapter = await factory.create(str(tss.integration_id))
    crm_org_id=tss.crm_org_id
    async with adapter:
        tickets_result = await adapter.fetch_tickets(crm_org_id)

    normalized = [
        _unified_to_normalized(t, source_system_name)
        for t in tickets_result.items
    ]

    async with async_session_maker() as db:
        try:
            result = await SyncService(db).sync_tickets(
                normalized_tickets = normalized,
                source_system      = source_system_name,
                tenant_id          = tenant_id,
            )
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


# ---------------------------------------------------------------------------
# Per-(tenant, source_system) orchestration
# ---------------------------------------------------------------------------

async def _sync_one_tenant_source_system(
    tenant_id: uuid.UUID,
    tss: TenantSourceSystem,
) -> dict:
    source_system_name = await _get_source_system_name(tss.source_system_id)
    if not source_system_name:
        return {"error": f"Unknown source_system_id={tss.source_system_id}"}

    crm_org_id = tss.crm_org_id

    logger.info(
        "Starting sync — tenant=%s source=%s crm_org_id=%s",
        tenant_id, source_system_name, crm_org_id,
    )

    try:
        factory = _get_factory()

        (
            agents_c, agents_u,
            customers_c, customers_u,
            companies_c, companies_u,
        ) = await _sync_entities_via_adapter(
            tenant_id, source_system_name, tss, factory
        )

        ticket_result = await _sync_tickets_via_adapter(
            tenant_id, source_system_name, tss, factory
        )

        logger.info(
            "Sync done — tenant=%s source=%s crm_org=%s | "
            "agents(%d/%d) customers(%d/%d) companies(%d/%d) "
            "tickets: fetched=%d created=%d updated=%d failed=%d",
            tenant_id, source_system_name, crm_org_id,
            agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
            ticket_result.total_fetched, ticket_result.created,
            ticket_result.updated, ticket_result.failed,
        )

        return _build_result(
            agents_c, agents_u, customers_c, customers_u,
            companies_c, companies_u, ticket_result,
        )

    except AdapterFactoryError as exc:
        logger.error(
            "Adapter factory error for tenant=%s source=%s crm_org=%s: %s",
            tenant_id, source_system_name, crm_org_id, exc,
        )
        return {"error": str(exc)}
    except CrmClientError as exc:
        logger.error(
            "CRM client error for tenant=%s source=%s crm_org=%s: %s",
            tenant_id, source_system_name, crm_org_id, exc,
        )
        return {"error": str(exc)}
    except Exception as exc:
        logger.exception(
            "Unexpected error for tenant=%s source=%s crm_org=%s: %s",
            tenant_id, source_system_name, crm_org_id, exc,
        )
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Main sync entry points
# ---------------------------------------------------------------------------

async def run_all_tenants_full_sync() -> None:
    tenants = await _get_all_active_tenants()
    logger.info("Scheduler: starting full sync for %d active tenants", len(tenants))
    for tenant in tenants:
        tss_rows = await _get_tenant_source_systems(tenant.id)
        if not tss_rows:
            logger.debug(
                "Tenant %s has no active source systems with crm_org_id — skipping",
                tenant.id,
            )
            continue
        for tss in tss_rows:
            await _sync_one_tenant_source_system(tenant.id, tss)
    logger.info("Scheduler: full multitenant sync complete")


async def run_tenant_full_sync(
    tenant_id: uuid.UUID,
    db: AsyncSession | None = None,
) -> dict:
    tss_rows = await _get_tenant_source_systems(tenant_id)
    if not tss_rows:
        logger.warning("Tenant %s has no active source systems with crm_org_id", tenant_id)
        return {}
    all_results: dict[str, dict] = {}
    for tss in tss_rows:
        source_system_name = await _get_source_system_name(tss.source_system_id)
        key = f"{source_system_name}:{tss.crm_org_id}"
        all_results[key] = await _sync_one_tenant_source_system(tenant_id, tss)
    return all_results


async def run_zammad_full_sync(db: AsyncSession | None = None) -> dict:
    tenants = await _get_all_active_tenants()
    all_results: dict = {}
    for tenant in tenants:
        tss_rows = await _get_tenant_source_systems(tenant.id)
        for tss in tss_rows:
            name = await _get_source_system_name(tss.source_system_id)
            if name != "zammad":
                continue
            key = f"tenant:{tenant.id}:zammad:{tss.crm_org_id}"
            all_results[key] = await _sync_one_tenant_source_system(tenant.id, tss)
    return all_results


async def run_espocrm_full_sync(db: AsyncSession | None = None) -> dict:
    tenants = await _get_all_active_tenants()
    all_results: dict = {}
    for tenant in tenants:
        tss_rows = await _get_tenant_source_systems(tenant.id)
        for tss in tss_rows:
            name = await _get_source_system_name(tss.source_system_id)
            if name != "espocrm":
                continue
            key = f"tenant:{tenant.id}:espocrm:{tss.crm_org_id}"
            all_results[key] = await _sync_one_tenant_source_system(tenant.id, tss)
    return all_results


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------

def _build_result(
    agents_c, agents_u,
    customers_c, customers_u,
    companies_c, companies_u,
    ticket_result,
) -> dict:
    return {
        "entities": {
            "agents":    {"created": agents_c,    "updated": agents_u},
            "customers": {"created": customers_c, "updated": customers_u},
            "companies": {"created": companies_c, "updated": companies_u},
        },
        "tickets": {
            "total_fetched": ticket_result.total_fetched,
            "created":       ticket_result.created,
            "updated":       ticket_result.updated,
            "failed":        ticket_result.failed,
        },
    }


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    scheduler.add_job(
        run_all_tenants_full_sync,
        trigger=IntervalTrigger(minutes=get_settings().SYNC_INTERVAL_MINUTES),
        id="full_crm_sync",
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info(
        "CRM sync scheduler started — interval=%d min",
        get_settings().SYNC_INTERVAL_MINUTES,
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("CRM sync scheduler stopped.")