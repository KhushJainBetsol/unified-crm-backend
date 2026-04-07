# app/services/scheduler.py

from __future__ import annotations

import logging

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
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Internal helpers — each uses its own isolated session so a failure in one
# step cannot leave a poisoned transaction for the next step.
# ---------------------------------------------------------------------------

async def _sync_zammad_entities(source_id: int, raw_agents, raw_customers, raw_orgs) -> tuple:
    """
    Sync Zammad agents / customers / companies in their own session.
    Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
    """
    async with async_session_maker() as db:
        try:
            svc = EntitySyncService(db, source_id)
            agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
            customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
            companies_c, companies_u = await svc.sync_zammad_companies(raw_orgs)
            await db.commit()
            return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
        except Exception:
            await db.rollback()
            raise


async def _sync_zammad_tickets(source_system: str, normalized) -> object:
    """
    Sync Zammad tickets in their own session.
    Returns a SyncResult.
    """
    async with async_session_maker() as db:
        try:
            result = await SyncService(db).sync_tickets(normalized, source_system=source_system)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


async def _sync_espo_entities(source_id: int, raw_agents, raw_customers, raw_companies) -> tuple:
    """
    Sync EspoCRM agents / customers / companies in their own session.
    Returns (agents_c, agents_u, customers_c, customers_u, companies_c, companies_u).
    """
    async with async_session_maker() as db:
        try:
            svc = EntitySyncService(db, source_id)
            agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
            customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
            companies_c, companies_u = await svc.sync_espo_companies(raw_companies)
            await db.commit()
            return agents_c, agents_u, customers_c, customers_u, companies_c, companies_u
        except Exception:
            await db.rollback()
            raise


async def _sync_espo_tickets(source_system: str, normalized) -> object:
    """
    Sync EspoCRM tickets in their own session.
    Returns a SyncResult.
    """
    async with async_session_maker() as db:
        try:
            result = await SyncService(db).sync_tickets(normalized, source_system=source_system)
            await db.commit()
            return result
        except Exception:
            await db.rollback()
            raise


async def _get_source_system_id(name: str) -> int | None:
    """
    Resolve source system name → DB id in its own short-lived session.
    Isolated so a failure here cannot affect any other session.
    """
    async with async_session_maker() as db:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == name.lower())
        )
        source = result.scalars().first()
        if not source:
            logger.error("Scheduler: source system '%s' not found, skipping.", name)
            return None
        return source.id


# ---------------------------------------------------------------------------
# Public sync functions
# When called from a route, caller passes its own db session and owns
# commit/rollback for that outer transaction.
# When called from the scheduler (db=None), each internal step owns its
# own session — a failure in one step never poisons another.
# ---------------------------------------------------------------------------

async def run_zammad_full_sync(db: AsyncSession | None = None) -> dict:
    """
    Full Zammad sync: fetch from API → sync entities → sync tickets.

    Session strategy (scheduler path, db=None):
      - Source system lookup  → isolated session (_get_source_system_id)
      - Entity sync           → isolated session (_sync_zammad_entities)
      - Ticket sync           → isolated session (_sync_zammad_tickets)

    This guarantees that a failure in entity sync cannot leave a poisoned
    transaction that blocks ticket sync.
    """
    # ── Fetch all raw data from Zammad API (no DB involved) ────────────────
    async with ZammadClient() as client:
        raw_agents    = await client.get_all_agents()
        raw_customers = await client.get_all_customers()
        raw_orgs      = await client.get_all_organizations()
        normalized    = await ZammadService(client).fetch_all_tickets()

    # ── Route path: caller owns the session ────────────────────────────────
    if db is not None:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == "zammad")
        )
        source = result.scalars().first()
        if not source:
            logger.error("Scheduler: source system 'zammad' not found, skipping.")
            return {}

        svc = EntitySyncService(db, source.id)
        agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
        customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
        companies_c, companies_u = await svc.sync_zammad_companies(raw_orgs)
        ticket_result = await SyncService(db).sync_tickets(normalized, source_system="zammad")

        # Caller owns commit — do not commit here
        return _build_result(
            agents_c, agents_u, customers_c, customers_u,
            companies_c, companies_u, ticket_result,
        )

    # ── Scheduler path: each step owns its own isolated session ────────────
    source_id = await _get_source_system_id("zammad")
    if not source_id:
        return {}

    (
        agents_c, agents_u,
        customers_c, customers_u,
        companies_c, companies_u,
    ) = await _sync_zammad_entities(source_id, raw_agents, raw_customers, raw_orgs)

    ticket_result = await _sync_zammad_tickets("zammad", normalized)

    logger.info(
        "Zammad full sync done — agents(%d/%d) customers(%d/%d) companies(%d/%d) "
        "tickets: fetched=%d created=%d updated=%d failed=%d",
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
        ticket_result.total_fetched, ticket_result.created,
        ticket_result.updated, ticket_result.failed,
    )

    return _build_result(
        agents_c, agents_u, customers_c, customers_u,
        companies_c, companies_u, ticket_result,
    )


async def run_espocrm_full_sync(db: AsyncSession | None = None) -> dict:
    """
    Full EspoCRM sync: fetch from API → sync entities → sync tickets.
    Same session isolation strategy as run_zammad_full_sync.
    """
    # ── Fetch all raw data from EspoCRM API (no DB involved) ───────────────
    async with EspoClient() as client:
        raw_agents    = await client.get_all_agents()
        raw_customers = await client.get_all_customers()
        raw_companies = await client.get_all_companies()
        normalized    = await EspoService(client).fetch_all_tickets()

    # ── Route path: caller owns the session ────────────────────────────────
    if db is not None:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == "espocrm")
        )
        source = result.scalars().first()
        if not source:
            logger.error("Scheduler: source system 'espocrm' not found, skipping.")
            return {}

        svc = EntitySyncService(db, source.id)
        agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
        customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
        companies_c, companies_u = await svc.sync_espo_companies(raw_companies)
        ticket_result = await SyncService(db).sync_tickets(normalized, source_system="espocrm")

        return _build_result(
            agents_c, agents_u, customers_c, customers_u,
            companies_c, companies_u, ticket_result,
        )

    # ── Scheduler path: each step owns its own isolated session ────────────
    source_id = await _get_source_system_id("espocrm")
    if not source_id:
        return {}

    (
        agents_c, agents_u,
        customers_c, customers_u,
        companies_c, companies_u,
    ) = await _sync_espo_entities(source_id, raw_agents, raw_customers, raw_companies)

    ticket_result = await _sync_espo_tickets("espocrm", normalized)

    logger.info(
        "EspoCRM full sync done — agents(%d/%d) customers(%d/%d) companies(%d/%d) "
        "tickets: fetched=%d created=%d updated=%d failed=%d",
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
        ticket_result.total_fetched, ticket_result.created,
        ticket_result.updated, ticket_result.failed,
    )

    return _build_result(
        agents_c, agents_u, customers_c, customers_u,
        companies_c, companies_u, ticket_result,
    )


async def run_all_full_sync() -> None:
    """Runs both CRMs. Errors in one don't abort the other."""
    for name, fn in [("Zammad", run_zammad_full_sync), ("EspoCRM", run_espocrm_full_sync)]:
        try:
            await fn()  # db=None → scheduler path, each step owns its session
        except (ZammadClientError, ZammadAuthError, EspoClientError, EspoAuthError) as exc:
            logger.error("Scheduled sync failed for %s: %s", name, exc)
        except Exception as exc:
            logger.exception("Unexpected error during scheduled sync for %s: %s", name, exc)


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
        run_all_full_sync,
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