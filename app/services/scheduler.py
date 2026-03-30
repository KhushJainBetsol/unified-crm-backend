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
# Core sync functions — accept optional db session
# If db is passed (from a route), caller owns commit/rollback.
# If db is None (from scheduler), we create and commit our own session.
# ---------------------------------------------------------------------------

async def run_zammad_full_sync(db: AsyncSession | None = None) -> dict:
    _owns_session = db is None
    if _owns_session:
        db = async_session_maker()
        await db.__aenter__()

    try:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == "zammad")
        )
        source = result.scalars().first()
        if not source:
            logger.error("Scheduler: source system 'zammad' not found, skipping.")
            return {}

        async with ZammadClient() as client:
            raw_agents    = await client.get_all_agents()
            raw_customers = await client.get_all_customers()
            raw_orgs      = await client.get_all_organizations()
            normalized    = await ZammadService(client).fetch_all_tickets()

        svc = EntitySyncService(db, source.id)
        agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
        customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
        companies_c, companies_u = await svc.sync_zammad_companies(raw_orgs)
        ticket_result = await SyncService(db).sync_tickets(normalized, source_system="zammad")

        if _owns_session:
            await db.commit()

        logger.info(
            "Zammad full sync done — agents(%d/%d) customers(%d/%d) companies(%d/%d) "
            "tickets: fetched=%d created=%d updated=%d failed=%d",
            agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
            ticket_result.total_fetched, ticket_result.created,
            ticket_result.updated, ticket_result.failed,
        )
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

    except Exception:
        if _owns_session:
            await db.rollback()
        raise

    finally:
        if _owns_session:
            await db.__aexit__(None, None, None)


async def run_espocrm_full_sync(db: AsyncSession | None = None) -> dict:
    _owns_session = db is None
    if _owns_session:
        db = async_session_maker()
        await db.__aenter__()

    try:
        result = await db.execute(
            select(SourceSystem).where(SourceSystem.system_name == "espocrm")
        )
        source = result.scalars().first()
        if not source:
            logger.error("Scheduler: source system 'espocrm' not found, skipping.")
            return {}

        async with EspoClient() as client:
            raw_agents    = await client.get_all_agents()
            raw_customers = await client.get_all_customers()
            raw_companies = await client.get_all_companies()
            normalized    = await EspoService(client).fetch_all_tickets()

        svc = EntitySyncService(db, source.id)
        agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
        customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
        companies_c, companies_u = await svc.sync_espo_companies(raw_companies)
        ticket_result = await SyncService(db).sync_tickets(normalized, source_system="espocrm")

        if _owns_session:
            await db.commit()

        logger.info(
            "EspoCRM full sync done — agents(%d/%d) customers(%d/%d) companies(%d/%d) "
            "tickets: fetched=%d created=%d updated=%d failed=%d",
            agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
            ticket_result.total_fetched, ticket_result.created,
            ticket_result.updated, ticket_result.failed,
        )
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

    except Exception:
        if _owns_session:
            await db.rollback()
        raise

    finally:
        if _owns_session:
            await db.__aexit__(None, None, None)


async def run_all_full_sync() -> None:
    """Runs both CRMs. Errors in one don't abort the other."""
    for name, fn in [("Zammad", run_zammad_full_sync), ("EspoCRM", run_espocrm_full_sync)]:
        try:
            await fn()  # no db passed → scheduler owns the session
        except (ZammadClientError, ZammadAuthError, EspoClientError, EspoAuthError) as exc:
            logger.error("Scheduled sync failed for %s: %s", name, exc)
        except Exception as exc:
            logger.exception("Unexpected error during scheduled sync for %s: %s", name, exc)


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
    logger.info("CRM sync scheduler started — interval=%d min", get_settings().SYNC_INTERVAL_MINUTES)


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
    logger.info("CRM sync scheduler stopped.")