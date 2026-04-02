# # app/routes/sync.py

# from __future__ import annotations

# import logging
# import uuid

# from fastapi import APIRouter, Depends, HTTPException, status
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.dependencies import get_db
# from app.integrations.espo.client import EspoAuthError, EspoClient, EspoClientError
# from app.integrations.espo.service import EspoService
# from app.integrations.zammad.client import ZammadAuthError, ZammadClient, ZammadClientError
# from app.integrations.zammad.service import ZammadService
# from app.models.source_system import SourceSystem
# from app.services.entity_sync_service import EntitySyncService
# from app.services.sync_service import SyncService
# from app.services.comment_service import CommentService
# from app.services.scheduler import run_zammad_full_sync, run_espocrm_full_sync
# from app.utils.response import success

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/sync", tags=["Sync"])


# # ---------------------------------------------------------------------------
# # Shared helpers
# # ---------------------------------------------------------------------------

# async def _get_source_system_id(name: str, db: AsyncSession) -> int:
#     result = await db.execute(
#         select(SourceSystem).where(SourceSystem.system_name == name)
#     )
#     source = result.scalars().first()
#     if not source:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"Source system '{name}' not found. Make sure it is seeded on startup.",
#         )
#     return source.id


# def _crm_error_to_http(exc: Exception) -> HTTPException:
#     if isinstance(exc, (ZammadAuthError, EspoAuthError)):
#         return HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail=f"CRM authentication failed. Check your API credentials in .env: {exc}",
#         )
#     return HTTPException(
#         status_code=status.HTTP_502_BAD_GATEWAY,
#         detail=f"CRM connection failed. Check the base URL in .env and ensure the CRM is reachable: {exc}",
#     )


# # ===========================================================================
# # ZAMMAD
# # ===========================================================================

# @router.post("/zammad/sync-entities", summary="Sync Zammad agents, customers, companies")
# async def sync_zammad_entities(db: AsyncSession = Depends(get_db)):
#     """Step 1 — run BEFORE ticket sync. Populates agents, customers, companies."""
#     source_system_id = await _get_source_system_id("zammad", db)

#     try:
#         async with ZammadClient() as client:
#             raw_agents    = await client.get_all_agents()
#             raw_customers = await client.get_all_customers()
#             raw_orgs      = await client.get_all_organizations()
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id)
#     agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_zammad_companies(raw_orgs)

#     logger.info(
#         "Zammad entity sync: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
#         agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
#     )
#     return success("Zammad entities synced", {
#         "agents":    {"created": agents_c,    "updated": agents_u},
#         "customers": {"created": customers_c, "updated": customers_u},
#         "companies": {"created": companies_c, "updated": companies_u},
#     })


# @router.post("/zammad/sync-tickets", summary="Sync Zammad tickets")
# async def sync_zammad_tickets(db: AsyncSession = Depends(get_db)):
#     """Step 2 — run AFTER /sync/zammad/entities."""
#     try:
#         async with ZammadClient() as client:
#             normalized = await ZammadService(client).fetch_all_tickets()
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     result = await SyncService(db).sync_tickets(normalized, source_system="zammad")
#     logger.info(
#         "Zammad ticket sync: fetched=%d created=%d updated=%d failed=%d",
#         result.total_fetched, result.created, result.updated, result.failed,
#     )

#     if result.failed > 0:
#         logger.warning(
#             "Zammad ticket sync had %d failures out of %d",
#             result.failed, result.total_fetched,
#         )

#     return success("Zammad ticket sync completed", {
#         "source_system": result.source_system,
#         "total_fetched": result.total_fetched,
#         "created":       result.created,
#         "updated":       result.updated,
#         "failed":        result.failed,
#     })


# @router.post("/zammad/full-sync", summary="Full Zammad sync — entities then tickets")
# async def sync_zammad_full(db: AsyncSession = Depends(get_db)):
#     """Runs entity sync then ticket sync in one call."""
#     try:
#         result = await run_zammad_full_sync(db)
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)
#     return success("Zammad full sync completed", result)


# # ===========================================================================
# # ESPOCRM
# # ===========================================================================

# @router.post("/espocrm/sync-entities", summary="Sync EspoCRM agents, customers, companies")
# async def sync_espocrm_entities(db: AsyncSession = Depends(get_db)):
#     """Step 1 — run BEFORE ticket sync."""
#     source_system_id = await _get_source_system_id("espocrm", db)

#     try:
#         async with EspoClient() as client:
#             raw_agents    = await client.get_all_agents()
#             raw_customers = await client.get_all_customers()
#             raw_companies = await client.get_all_companies()
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id)
#     agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_espo_companies(raw_companies)

#     logger.info(
#         "EspoCRM entity sync: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
#         agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
#     )
#     return success("EspoCRM entities synced", {
#         "agents":    {"created": agents_c,    "updated": agents_u},
#         "customers": {"created": customers_c, "updated": customers_u},
#         "companies": {"created": companies_c, "updated": companies_u},
#     })


# @router.post("/espocrm/sync-tickets", summary="Sync EspoCRM tickets")
# async def sync_espocrm_tickets(db: AsyncSession = Depends(get_db)):
#     """Step 2 — run AFTER /sync/espocrm/entities."""
#     try:
#         async with EspoClient() as client:
#             normalized = await EspoService(client).fetch_all_tickets()
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     result = await SyncService(db).sync_tickets(normalized, source_system="espocrm")
#     logger.info(
#         "EspoCRM ticket sync: fetched=%d created=%d updated=%d failed=%d",
#         result.total_fetched, result.created, result.updated, result.failed,
#     )

#     if result.failed > 0:
#         logger.warning(
#             "EspoCRM ticket sync had %d failures out of %d",
#             result.failed, result.total_fetched,
#         )

#     return success("EspoCRM ticket sync completed", {
#         "source_system": result.source_system,
#         "total_fetched": result.total_fetched,
#         "created":       result.created,
#         "updated":       result.updated,
#         "failed":        result.failed,
#     })


# @router.post("/espocrm/full-sync", summary="Full EspoCRM sync — entities then tickets")
# async def sync_espocrm_full(db: AsyncSession = Depends(get_db)):
#     """Runs entity sync then ticket sync in one call."""
#     try:
#         result = await run_espocrm_full_sync(db)
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)
#     return success("EspoCRM full sync completed", result)


# # ---------------------------------------------------------------------------
# # POST /tickets/{ticket_id}/comments/sync
# # ---------------------------------------------------------------------------

# @router.post(
#     "/{ticket_id}/comments/sync",
#     summary="Sync comments for a ticket from its CRM",
#     description=(
#         "Fetches comments from the ticket's source CRM (Zammad or EspoCRM), "
#         "normalizes them, and upserts into the ticket_comments table. "
#         "Source system is determined automatically from the ticket record. "
#         "Safe to call multiple times — uses upsert (no duplicates)."
#     ),
# )
# async def sync_ticket_comments(
#     ticket_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     count = await CommentService(db).sync_comments_for_ticket(ticket_id)
#     return success(
#         f"Synced {count} comment(s) for ticket {ticket_id}",
#         {"ticket_id": str(ticket_id), "synced_count": count},
#     )

# app/routes/sync.py

# app/routes/sync.py

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.integrations.espo.client import EspoAuthError, EspoClient, EspoClientError
from app.integrations.espo.service import EspoService
from app.integrations.zammad.client import ZammadAuthError, ZammadClient, ZammadClientError
from app.integrations.zammad.service import ZammadService
from app.models.source_system import SourceSystem
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService
from app.services.comment_service import CommentService
from app.services.scheduler import run_zammad_full_sync, run_espocrm_full_sync
from app.utils.response import success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["Sync"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _get_source_system_id(name: str, db: AsyncSession) -> int:
    result = await db.execute(
        select(SourceSystem).where(SourceSystem.system_name == name)
    )
    source = result.scalars().first()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source system '{name}' not found. Make sure it is seeded on startup.",
        )
    return source.id


def _crm_error_to_http(exc: Exception) -> HTTPException:
    if isinstance(exc, (ZammadAuthError, EspoAuthError)):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"CRM authentication failed. Check your API credentials in .env: {exc}",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"CRM connection failed. Check the base URL in .env and ensure the CRM is reachable: {exc}",
    )


# ===========================================================================
# ZAMMAD
# ===========================================================================

@router.post("/zammad/sync-entities", summary="Sync Zammad agents, customers, companies")
async def sync_zammad_entities(db: AsyncSession = Depends(get_db)):
    """Step 1 — run BEFORE ticket sync. Populates agents, customers, companies."""
    source_system_id = await _get_source_system_id("zammad", db)

    try:
        async with ZammadClient() as client:
            raw_agents    = await client.get_all_agents()
            raw_customers = await client.get_all_customers()
            raw_orgs      = await client.get_all_organizations()
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id)
    agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
    customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
    companies_c, companies_u = await svc.sync_zammad_companies(raw_orgs)

    logger.info(
        "Zammad entity sync: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
    )
    return success("Zammad entities synced", {
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    })


@router.post("/zammad/sync-tickets", summary="Sync Zammad tickets")
async def sync_zammad_tickets(db: AsyncSession = Depends(get_db)):
    """Step 2 — run AFTER /sync/zammad/entities."""
    try:
        async with ZammadClient() as client:
            normalized = await ZammadService(client).fetch_all_tickets()
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)

    result = await SyncService(db).sync_tickets(normalized, source_system="zammad")
    logger.info(
        "Zammad ticket sync: fetched=%d created=%d updated=%d failed=%d",
        result.total_fetched, result.created, result.updated, result.failed,
    )

    if result.failed > 0:
        logger.warning(
            "Zammad ticket sync had %d failures out of %d",
            result.failed, result.total_fetched,
        )

    return success("Zammad ticket sync completed", {
        "source_system": result.source_system,
        "total_fetched": result.total_fetched,
        "created":       result.created,
        "updated":       result.updated,
        "failed":        result.failed,
    })


@router.post("/zammad/full-sync", summary="Full Zammad sync — entities then tickets")
async def sync_zammad_full(db: AsyncSession = Depends(get_db)):
    """Runs entity sync then ticket sync in one call."""
    try:
        result = await run_zammad_full_sync(db)
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)
    return success("Zammad full sync completed", result)


# ===========================================================================
# ESPOCRM
# ===========================================================================

@router.post("/espocrm/sync-entities", summary="Sync EspoCRM agents, customers, companies")
async def sync_espocrm_entities(db: AsyncSession = Depends(get_db)):
    """Step 1 — run BEFORE ticket sync."""
    source_system_id = await _get_source_system_id("espocrm", db)

    try:
        async with EspoClient() as client:
            raw_agents    = await client.get_all_agents()
            raw_customers = await client.get_all_customers()
            raw_companies = await client.get_all_companies()
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id)
    agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
    customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
    companies_c, companies_u = await svc.sync_espo_companies(raw_companies)

    logger.info(
        "EspoCRM entity sync: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
    )
    return success("EspoCRM entities synced", {
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    })


@router.post("/espocrm/sync-tickets", summary="Sync EspoCRM tickets")
async def sync_espocrm_tickets(db: AsyncSession = Depends(get_db)):
    """Step 2 — run AFTER /sync/espocrm/entities."""
    try:
        async with EspoClient() as client:
            normalized = await EspoService(client).fetch_all_tickets()
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)

    result = await SyncService(db).sync_tickets(normalized, source_system="espocrm")
    logger.info(
        "EspoCRM ticket sync: fetched=%d created=%d updated=%d failed=%d",
        result.total_fetched, result.created, result.updated, result.failed,
    )

    if result.failed > 0:
        logger.warning(
            "EspoCRM ticket sync had %d failures out of %d",
            result.failed, result.total_fetched,
        )

    return success("EspoCRM ticket sync completed", {
        "source_system": result.source_system,
        "total_fetched": result.total_fetched,
        "created":       result.created,
        "updated":       result.updated,
        "failed":        result.failed,
    })


@router.post("/espocrm/full-sync", summary="Full EspoCRM sync — entities then tickets")
async def sync_espocrm_full(db: AsyncSession = Depends(get_db)):
    """Runs entity sync then ticket sync in one call."""
    try:
        result = await run_espocrm_full_sync(db)
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)
    return success("EspoCRM full sync completed", result)


# ---------------------------------------------------------------------------
# POST /tickets/{ticket_id}/comments/sync
# ---------------------------------------------------------------------------

@router.post(
    "/{ticket_id}/comments/sync",
    summary="Sync comments for a ticket from its CRM",
    description=(
        "Fetches comments from the ticket's source CRM (Zammad or EspoCRM), "
        "normalizes them, and upserts into the ticket_comments table. "
        "Source system is determined automatically from the ticket record. "
        "Safe to call multiple times — uses upsert (no duplicates)."
    ),
)
async def sync_ticket_comments(
    ticket_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    count = await CommentService(db).sync_comments_for_ticket(ticket_id)
    return success(
        f"Synced {count} comment(s) for ticket {ticket_id}",
        {"ticket_id": str(ticket_id), "synced_count": count},
    )