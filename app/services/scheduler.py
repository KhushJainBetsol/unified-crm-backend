"""
app/services/scheduler.py

Multitenant CRM sync scheduler.

Sync loop (per tick):
  1. Load all active tenants from DB.
  2. For each tenant, load their active tenant_source_systems rows
     (those with a non-NULL crm_org_id).
  3. For each (tenant, source_system, crm_org_id):
       a. Fetch agents / customers / company scoped to that org from the CRM.
       b. Fetch tickets scoped to that org from the CRM.
       c. Normalize everything.
       d. Upsert with tenant_id stamped — isolated DB session per step.
  4. Move to the next pair.

Session isolation strategy (scheduler path):
  Each step (entity sync, ticket sync) uses its own isolated async session.
  A failure in one tenant/step cannot poison the transaction of another.

Route path (db != None):
  The caller owns the session and commits. Used by manual trigger endpoints.
"""

from __future__ import annotations

import logging
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.core.settings import get_settings
from app.integrations.espo.client import EspoAuthError, EspoClient, EspoClientError
from app.integrations.espo.service import EspoService
from app.integrations.zammad.client import ZammadAuthError, ZammadClient, ZammadClientError
from app.integrations.zammad.service import ZammadService
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_source_systems import TenantSourceSystem
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# DB helpers — each uses its own short-lived session
# ---------------------------------------------------------------------------

async def _get_all_active_tenants() -> list[Tenant]:
    """Return all active tenants from DB."""
    async with async_session_maker() as db:
        result = await db.execute(
            select(Tenant).where(Tenant.is_active == True)  # noqa: E712
        )
        return list(result.scalars().all())


async def _get_tenant_source_systems(
    tenant_id: uuid.UUID,
) -> list[TenantSourceSystem]:
    """
    Return active tenant_source_systems rows for a tenant that have a
    resolved crm_org_id (NULL means the CRM org lookup hasn't succeeded yet).
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(TenantSourceSystem).where(
                TenantSourceSystem.tenant_id  == tenant_id,
                TenantSourceSystem.is_active  == True,          # noqa: E712
                TenantSourceSystem.crm_org_id != None,          # noqa: E711
            )
        )
        return list(result.scalars().all())


async def _get_source_system_name(source_system_id: int) -> str | None:
    """Resolve source_system_id → system_name."""
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
# Per-tenant per-source-system sync helpers
# Each function owns its own isolated session.
# ---------------------------------------------------------------------------

async def _sync_zammad_entities_for_tenant(
    tenant_id: uuid.UUID,
    source_system_id: int,
    crm_org_id: str,
) -> tuple[int, int, int, int, int, int]:
    """
    Fetch and upsert Zammad agents, customers, and the single company
    that corresponds to crm_org_id — all scoped to tenant_id.

    Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
    """
    async with ZammadClient() as client:
        raw_agents    = await client.get_agents_by_org(crm_org_id)
        raw_customers = await client.get_customers_by_org(crm_org_id)
        raw_org       = await client.get_organization_by_id(crm_org_id)

    async with async_session_maker() as db:
        try:
            svc = EntitySyncService(db, source_system_id, tenant_id)
            agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
            customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
            # Sync just this tenant's single org as a company record
            companies_c, companies_u = await svc.sync_zammad_companies([raw_org])
            await db.commit()
            return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
        except Exception:
            await db.rollback()
            raise


async def _sync_zammad_tickets_for_tenant(
    tenant_id: uuid.UUID,
    source_system_id: int,
    crm_org_id: str,
) -> object:
    """
    Fetch Zammad tickets scoped to crm_org_id, normalize, upsert for tenant.
    Returns a SyncResult.
    """
    async with ZammadClient() as client:
        raw_tickets = await client.get_tickets_by_org(crm_org_id)
        normalized  = ZammadService(client).normalize_raw_tickets(raw_tickets)

    async with async_session_maker() as db:
        try:
            result = await SyncService(db).sync_tickets(
                normalized_tickets = normalized,
                source_system      = "zammad",
                tenant_id          = tenant_id,
            )
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


async def _sync_espo_entities_for_tenant(
    tenant_id: uuid.UUID,
    source_system_id: int,
    crm_org_id: str,
) -> tuple[int, int, int, int, int, int]:
    """
    Fetch and upsert EspoCRM agents, contacts (customers), and the single
    Account that corresponds to crm_org_id — all scoped to tenant_id.

    Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
    """
    async with EspoClient() as client:
        raw_agents    = await client.get_agents_by_account(crm_org_id)
        raw_customers = await client.get_contacts_by_account(crm_org_id)
        raw_account   = await client.get_account_by_id(crm_org_id)

    async with async_session_maker() as db:
        try:
            svc = EntitySyncService(db, source_system_id, tenant_id)
            agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
            customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
            # Sync just this tenant's single account as a company record
            companies_c, companies_u = await svc.sync_espo_companies([raw_account])
            await db.commit()
            return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
        except Exception:
            await db.rollback()
            raise


async def _sync_espo_tickets_for_tenant(
    tenant_id: uuid.UUID,
    source_system_id: int,
    crm_org_id: str,
) -> object:
    """
    Fetch EspoCRM tickets scoped to crm_org_id, normalize, upsert for tenant.
    Returns a SyncResult.
    """
    async with EspoClient() as client:
        raw_tickets = await client.get_tickets_by_account(crm_org_id)
        normalized  = EspoService(client).normalize_raw_tickets(raw_tickets)

    async with async_session_maker() as db:
        try:
            result = await SyncService(db).sync_tickets(
                normalized_tickets = normalized,
                source_system      = "espocrm",
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
    """
    Run a full sync (entities + tickets) for one (tenant, source_system, crm_org_id).
    Returns a result dict. Never raises — errors are logged and returned in the dict.
    """
    source_system_name = await _get_source_system_name(tss.source_system_id)
    if not source_system_name:
        return {"error": f"Unknown source_system_id={tss.source_system_id}"}

    crm_org_id = tss.crm_org_id

    logger.info(
        "Starting sync — tenant=%s source=%s crm_org_id=%s",
        tenant_id, source_system_name, crm_org_id,
    )

    try:
        if source_system_name == "zammad":
            (
                agents_c, agents_u,
                customers_c, customers_u,
                companies_c, companies_u,
            ) = await _sync_zammad_entities_for_tenant(
                tenant_id, tss.source_system_id, crm_org_id
            )
            ticket_result = await _sync_zammad_tickets_for_tenant(
                tenant_id, tss.source_system_id, crm_org_id
            )

        elif source_system_name == "espocrm":
            (
                agents_c, agents_u,
                customers_c, customers_u,
                companies_c, companies_u,
            ) = await _sync_espo_entities_for_tenant(
                tenant_id, tss.source_system_id, crm_org_id
            )
            ticket_result = await _sync_espo_tickets_for_tenant(
                tenant_id, tss.source_system_id, crm_org_id
            )

        else:
            logger.warning(
                "No sync handler for source_system='%s' — skipping tenant=%s",
                source_system_name, tenant_id,
            )
            return {"skipped": True, "reason": f"No handler for {source_system_name}"}

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

    except (ZammadClientError, ZammadAuthError, EspoClientError, EspoAuthError) as exc:
        logger.error(
            "CRM error for tenant=%s source=%s crm_org=%s: %s",
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
    """
    Full multitenant sync.

    For every active tenant → for every active source system with a resolved
    crm_org_id → fetch + upsert entities + tickets scoped to that org.
    """
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
    """
    Full sync for a single tenant across all its registered source systems.

    When called from a route (db != None), the caller owns the session.
    When called from the scheduler (db=None), each step uses its own session.
    """
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
    """
    Backward-compatible: sync all tenants that have a Zammad connection.
    Scheduler path uses isolated sessions; route path (db != None) is
    handled per-tenant inside _sync_one_tenant_source_system.
    """
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
    """
    Backward-compatible: sync all tenants that have an EspoCRM connection.
    """
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
# Private helper — shared result shape
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