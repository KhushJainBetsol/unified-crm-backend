# """
# app/routers/sync.py

# Key change vs previous adapter-pattern version:
#   Removed all references to tss.integration_id — TenantSourceSystem has
#   no such field.  Entity and ticket sync routes now delegate to the same
#   scheduler helpers (_sync_one_tenant_source_system etc.) that the
#   full-sync route already uses successfully, keeping everything consistent.
# """

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
# from app.models.tenant import Tenant
# from app.models.tenant_source_systems import TenantSourceSystem
# from app.services.entity_sync_service import EntitySyncService
# from app.services.sync_service import SyncService
# from app.services.comment_service import CommentService
# from app.services.scheduler import (
#     run_tenant_full_sync,
#     run_zammad_full_sync,
#     run_espocrm_full_sync,
#     _get_source_system_name,
# )
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


# async def _get_tenant_or_404(tenant_id: uuid.UUID, db: AsyncSession) -> Tenant:
#     result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
#     tenant = result.scalars().first()
#     if not tenant:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=f"Tenant '{tenant_id}' not found.",
#         )
#     return tenant


# async def _get_tss_or_404(
#     tenant_id: uuid.UUID,
#     source_system_name: str,
#     db: AsyncSession,
# ) -> TenantSourceSystem:
#     source_system_id = await _get_source_system_id(source_system_name, db)
#     result = await db.execute(
#         select(TenantSourceSystem).where(
#             TenantSourceSystem.tenant_id        == tenant_id,
#             TenantSourceSystem.source_system_id == source_system_id,
#             TenantSourceSystem.is_active        == True,  # noqa: E712
#         )
#     )
#     tss = result.scalars().first()
#     if not tss:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail=(
#                 f"No active {source_system_name} integration found for tenant '{tenant_id}'. "
#                 "Make sure the integration is registered and active."
#             ),
#         )
#     if not tss.crm_org_id:
#         raise HTTPException(
#             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
#             detail=(
#                 f"crm_org_id is not yet resolved for tenant '{tenant_id}' / {source_system_name}. "
#                 "The CRM org lookup must succeed before syncing."
#             ),
#         )
#     return tss


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
# # TENANT-SCOPED FULL SYNC
# # ===========================================================================

# @router.post(
#     "/tenant/{tenant_id}/full-sync",
#     summary="Full sync for a single tenant — all its registered CRM systems",
# )
# async def sync_tenant_full(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     result = await run_tenant_full_sync(tenant_id, db=None)
#     return success(f"Full sync completed for tenant {tenant_id}", result)


# # ===========================================================================
# # ZAMMAD — tenant-scoped
# # ===========================================================================

# @router.post(
#     "/tenant/{tenant_id}/zammad/sync-entities",
#     summary="Sync Zammad agents, customers, company for one tenant",
# )
# async def sync_zammad_entities_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss              = await _get_tss_or_404(tenant_id, "zammad", db)
#     source_system_id = tss.source_system_id
#     crm_org_id       = tss.crm_org_id

#     try:
#         async with ZammadClient() as client:
#             raw_agents    = await client.get_agents_by_org(crm_org_id)
#             raw_customers = await client.get_customers_by_org(crm_org_id)
#             raw_org       = await client.get_organization_by_id(crm_org_id)
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id, tenant_id)
#     agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_zammad_companies([raw_org])

#     logger.info(
#         "Zammad entity sync tenant=%s org=%s: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
#         tenant_id, crm_org_id,
#         agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
#     )
#     return success("Zammad entities synced", {
#         "tenant_id":  str(tenant_id),
#         "crm_org_id": crm_org_id,
#         "agents":    {"created": agents_c,    "updated": agents_u},
#         "customers": {"created": customers_c, "updated": customers_u},
#         "companies": {"created": companies_c, "updated": companies_u},
#     })


# @router.post(
#     "/tenant/{tenant_id}/zammad/sync-tickets",
#     summary="Sync Zammad tickets for one tenant",
# )
# async def sync_zammad_tickets_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss        = await _get_tss_or_404(tenant_id, "zammad", db)
#     crm_org_id = tss.crm_org_id

#     try:
#         async with ZammadClient() as client:
#             raw_tickets = await client.get_tickets_by_org(crm_org_id)
#             normalized  = ZammadService(client).normalize_raw_tickets(raw_tickets)
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     result = await SyncService(db).sync_tickets(
#         normalized_tickets = normalized,
#         source_system      = "zammad",
#         tenant_id          = tenant_id,
#     )
#     logger.info(
#         "Zammad ticket sync tenant=%s org=%s: fetched=%d created=%d updated=%d failed=%d",
#         tenant_id, crm_org_id,
#         result.total_fetched, result.created, result.updated, result.failed,
#     )
#     return success("Zammad ticket sync completed", {
#         "tenant_id":     str(tenant_id),
#         "crm_org_id":    crm_org_id,
#         "source_system": result.source_system,
#         "total_fetched": result.total_fetched,
#         "created":       result.created,
#         "updated":       result.updated,
#         "failed":        result.failed,
#     })


# @router.post(
#     "/tenant/{tenant_id}/zammad/full-sync",
#     summary="Full Zammad sync for one tenant — entities then tickets",
# )
# async def sync_zammad_full_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss              = await _get_tss_or_404(tenant_id, "zammad", db)
#     source_system_id = tss.source_system_id
#     crm_org_id       = tss.crm_org_id

#     try:
#         async with ZammadClient() as client:
#             raw_agents    = await client.get_agents_by_org(crm_org_id)
#             raw_customers = await client.get_customers_by_org(crm_org_id)
#             raw_org       = await client.get_organization_by_id(crm_org_id)
#             raw_tickets   = await client.get_tickets_by_org(crm_org_id)
#             normalized    = ZammadService(client).normalize_raw_tickets(raw_tickets)
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id, tenant_id)
#     agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_zammad_companies([raw_org])

#     ticket_result = await SyncService(db).sync_tickets(
#         normalized_tickets = normalized,
#         source_system      = "zammad",
#         tenant_id          = tenant_id,
#     )

#     return success("Zammad full sync completed", {
#         "tenant_id":  str(tenant_id),
#         "crm_org_id": crm_org_id,
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
#     })


# # ===========================================================================
# # ESPOCRM — tenant-scoped
# # ===========================================================================

# @router.post(
#     "/tenant/{tenant_id}/espocrm/sync-entities",
#     summary="Sync EspoCRM agents, customers, company for one tenant",
# )
# async def sync_espocrm_entities_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss              = await _get_tss_or_404(tenant_id, "espocrm", db)
#     source_system_id = tss.source_system_id
#     crm_org_id       = tss.crm_org_id

#     try:
#         async with EspoClient() as client:
#             raw_agents    = await client.get_agents_by_account(crm_org_id)
#             raw_customers = await client.get_contacts_by_account(crm_org_id)
#             raw_account   = await client.get_account_by_id(crm_org_id)
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id, tenant_id)
#     agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_espo_companies([raw_account])

#     logger.info(
#         "EspoCRM entity sync tenant=%s account=%s: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
#         tenant_id, crm_org_id,
#         agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
#     )
#     return success("EspoCRM entities synced", {
#         "tenant_id":  str(tenant_id),
#         "crm_org_id": crm_org_id,
#         "agents":    {"created": agents_c,    "updated": agents_u},
#         "customers": {"created": customers_c, "updated": customers_u},
#         "companies": {"created": companies_c, "updated": companies_u},
#     })


# @router.post(
#     "/tenant/{tenant_id}/espocrm/sync-tickets",
#     summary="Sync EspoCRM tickets for one tenant",
# )
# async def sync_espocrm_tickets_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss        = await _get_tss_or_404(tenant_id, "espocrm", db)
#     crm_org_id = tss.crm_org_id

#     try:
#         async with EspoClient() as client:
#             raw_tickets = await client.get_tickets_by_account(crm_org_id)
#             normalized  = EspoService(client).normalize_raw_tickets(raw_tickets)
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     result = await SyncService(db).sync_tickets(
#         normalized_tickets = normalized,
#         source_system      = "espocrm",
#         tenant_id          = tenant_id,
#     )
#     logger.info(
#         "EspoCRM ticket sync tenant=%s account=%s: fetched=%d created=%d updated=%d failed=%d",
#         tenant_id, crm_org_id,
#         result.total_fetched, result.created, result.updated, result.failed,
#     )
#     return success("EspoCRM ticket sync completed", {
#         "tenant_id":     str(tenant_id),
#         "crm_org_id":    crm_org_id,
#         "source_system": result.source_system,
#         "total_fetched": result.total_fetched,
#         "created":       result.created,
#         "updated":       result.updated,
#         "failed":        result.failed,
#     })


# @router.post(
#     "/tenant/{tenant_id}/espocrm/full-sync",
#     summary="Full EspoCRM sync for one tenant — entities then tickets",
# )
# async def sync_espocrm_full_for_tenant(
#     tenant_id: uuid.UUID,
#     db: AsyncSession = Depends(get_db),
# ):
#     await _get_tenant_or_404(tenant_id, db)
#     tss              = await _get_tss_or_404(tenant_id, "espocrm", db)
#     source_system_id = tss.source_system_id
#     crm_org_id       = tss.crm_org_id

#     try:
#         async with EspoClient() as client:
#             raw_agents    = await client.get_agents_by_account(crm_org_id)
#             raw_customers = await client.get_contacts_by_account(crm_org_id)
#             raw_account   = await client.get_account_by_id(crm_org_id)
#             raw_tickets   = await client.get_tickets_by_account(crm_org_id)
#             normalized    = EspoService(client).normalize_raw_tickets(raw_tickets)
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)

#     svc = EntitySyncService(db, source_system_id, tenant_id)
#     agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
#     customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
#     companies_c, companies_u = await svc.sync_espo_companies([raw_account])

#     ticket_result = await SyncService(db).sync_tickets(
#         normalized_tickets = normalized,
#         source_system      = "espocrm",
#         tenant_id          = tenant_id,
#     )

#     return success("EspoCRM full sync completed", {
#         "tenant_id":  str(tenant_id),
#         "crm_org_id": crm_org_id,
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
#     })


# # ===========================================================================
# # LEGACY — kept for backward compat
# # ===========================================================================

# @router.post("/zammad/full-sync", summary="Full Zammad sync — all tenants")
# async def sync_zammad_full(db: AsyncSession = Depends(get_db)):
#     try:
#         result = await run_zammad_full_sync(db=None)
#     except (ZammadClientError, ZammadAuthError) as exc:
#         raise _crm_error_to_http(exc)
#     return success("Zammad full sync completed (all tenants)", result)


# @router.post("/espocrm/full-sync", summary="Full EspoCRM sync — all tenants")
# async def sync_espocrm_full(db: AsyncSession = Depends(get_db)):
#     try:
#         result = await run_espocrm_full_sync(db=None)
#     except (EspoClientError, EspoAuthError) as exc:
#         raise _crm_error_to_http(exc)
#     return success("EspoCRM full sync completed (all tenants)", result)


# # ---------------------------------------------------------------------------
# # POST /sync/{ticket_id}/comments/sync
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

"""
app/routers/sync.py

Adapter-pattern sync routes.

All CRM-specific imports (EspoClient, ZammadClient, EspoService,
ZammadService) are gone.  Every sync operation now goes through the
CrmAdapterFactory, which reads endpoints and credentials from the YAML
config and the credential store.

Entity sync (agents / customers / companies) is handled by the
adapter-aware EntitySyncService which calls the adapter's
fetch_agents() / fetch_organizations() methods.

Ticket sync uses the adapter's fetch_tickets() method and the
config-driven normalizer registry.

The public URL structure is unchanged so existing clients are unaffected:

  POST /sync/tenant/{tenant_id}/full-sync
  POST /sync/tenant/{tenant_id}/{crm_type}/sync-entities
  POST /sync/tenant/{tenant_id}/{crm_type}/sync-tickets
  POST /sync/tenant/{tenant_id}/{crm_type}/full-sync
  POST /sync/{ticket_id}/comments/sync

Legacy CRM-specific endpoints are kept for backward compatibility but
delegate to the generic adapter path.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapter_dependencies.deps import get_adapter_factory, get_adapter_registry
from app.config.registry import AdapterNotFoundError, AdapterRegistry
from app.dependencies import get_db
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory
from app.integrations.normalizer import normalize_tickets_with_registry
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_source_systems import TenantSourceSystem
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService
from app.services.comment_service import CommentService
from app.services.scheduler import (
    run_tenant_full_sync,
    run_zammad_full_sync,
    run_espocrm_full_sync,
)
from app.utils.response import success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["Sync"])


# ---------------------------------------------------------------------------
# Shared DB helpers  (unchanged)
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


async def _get_tenant_or_404(tenant_id: uuid.UUID, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalars().first()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found.",
        )
    return tenant


async def _get_tss_or_404(
    tenant_id: uuid.UUID,
    source_system_name: str,
    db: AsyncSession,
) -> TenantSourceSystem:
    source_system_id = await _get_source_system_id(source_system_name, db)
    result = await db.execute(
        select(TenantSourceSystem).where(
            TenantSourceSystem.tenant_id        == tenant_id,
            TenantSourceSystem.source_system_id == source_system_id,
            TenantSourceSystem.is_active        == True,  # noqa: E712
        )
    )
    tss = result.scalars().first()
    if not tss:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active {source_system_name} integration found for tenant '{tenant_id}'. "
                "Make sure the integration is registered and active."
            ),
        )
    if not tss.crm_org_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"crm_org_id is not yet resolved for tenant '{tenant_id}' / {source_system_name}. "
                "The CRM org lookup must succeed before syncing."
            ),
        )
    return tss


def _adapter_error_to_http(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"CRM connection failed: {exc}",
    )


# ---------------------------------------------------------------------------
# Generic adapter-backed sync helpers
# ---------------------------------------------------------------------------

async def _sync_entities_via_adapter(
    tenant_id: uuid.UUID,
    crm_type: str,
    tss: TenantSourceSystem,
    factory: CrmAdapterFactory,
    db: AsyncSession,
) -> dict:
    """
    Fetch agents, customers, and company from the CRM via the adapter
    and upsert them into the DB.  Returns a summary dict.
    """
    source_system_id = tss.source_system_id
    crm_org_id       = tss.crm_org_id

    try:
        adapter = await factory.create(str(tss.integration_id))
        async with adapter:
            raw_agents    = await adapter.fetch_agents()
            raw_customers = await adapter.fetch_customers(org_id=crm_org_id)
            raw_orgs      = await adapter.fetch_organizations()
    except AdapterFactoryError as exc:
        raise _adapter_error_to_http(exc)
    except Exception as exc:
        raise _adapter_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_agents(raw_agents,    crm_type)
    customers_c, customers_u = await svc.sync_customers(raw_customers, crm_type)
    companies_c, companies_u = await svc.sync_companies(raw_orgs,    crm_type)

    return {
        "crm_org_id": crm_org_id,
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    }


async def _sync_tickets_via_adapter(
    tenant_id: uuid.UUID,
    crm_type: str,
    tss: TenantSourceSystem,
    factory: CrmAdapterFactory,
    registry: AdapterRegistry,
    db: AsyncSession,
) -> dict:
    """
    Fetch tickets from the CRM via the adapter, normalize via the
    config-driven normalizer, and upsert into the DB.
    Returns a summary dict.
    """
    crm_org_id = tss.crm_org_id

    try:
        adapter = await factory.create(str(tss.integration_id))
        async with adapter:
            raw_tickets = await adapter.fetch_tickets(org_id=crm_org_id)
    except AdapterFactoryError as exc:
        raise _adapter_error_to_http(exc)
    except Exception as exc:
        raise _adapter_error_to_http(exc)

    normalized = normalize_tickets_with_registry(
        raw_list=raw_tickets,
        source_system=crm_type,
        registry=registry,
    )

    result = await SyncService(db).sync_tickets(
        normalized_tickets=normalized,
        source_system=crm_type,
        tenant_id=tenant_id,
    )

    logger.info(
        "%s ticket sync tenant=%s org=%s: fetched=%d created=%d updated=%d failed=%d",
        crm_type, tenant_id, crm_org_id,
        result.total_fetched, result.created, result.updated, result.failed,
    )

    return {
        "crm_org_id":    crm_org_id,
        "source_system": result.source_system,
        "total_fetched": result.total_fetched,
        "created":       result.created,
        "updated":       result.updated,
        "failed":        result.failed,
    }


# ===========================================================================
# TENANT-SCOPED FULL SYNC
# ===========================================================================

@router.post(
    "/tenant/{tenant_id}/full-sync",
    summary="Full sync for a single tenant — all its registered CRM systems",
)
async def sync_tenant_full(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    await _get_tenant_or_404(tenant_id, db)
    result = await run_tenant_full_sync(tenant_id, db=None)
    return success(f"Full sync completed for tenant {tenant_id}", result)


# ===========================================================================
# GENERIC CRM-TYPE ROUTES  (work for any registered adapter)
# ===========================================================================

@router.post(
    "/tenant/{tenant_id}/{crm_type}/sync-entities",
    summary="Sync agents, customers, company for one tenant (any CRM)",
)
async def sync_entities_for_tenant(
    tenant_id: uuid.UUID,
    crm_type: str,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)

    # Validate crm_type is registered
    try:
        registry.get_entry(crm_type)
    except AdapterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown CRM type '{crm_type}'. Available: {registry.list_adapter_keys()}",
        )

    tss = await _get_tss_or_404(tenant_id, crm_type, db)
    entity_result = await _sync_entities_via_adapter(
        tenant_id, crm_type, tss, factory, db
    )

    logger.info(
        "%s entity sync tenant=%s: %s", crm_type, tenant_id, entity_result
    )
    return success(f"{crm_type} entities synced", {"tenant_id": str(tenant_id), **entity_result})


@router.post(
    "/tenant/{tenant_id}/{crm_type}/sync-tickets",
    summary="Sync tickets for one tenant (any CRM)",
)
async def sync_tickets_for_tenant(
    tenant_id: uuid.UUID,
    crm_type: str,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)

    try:
        registry.get_entry(crm_type)
    except AdapterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown CRM type '{crm_type}'. Available: {registry.list_adapter_keys()}",
        )

    tss = await _get_tss_or_404(tenant_id, crm_type, db)
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, crm_type, tss, factory, registry, db
    )

    return success(f"{crm_type} ticket sync completed", {"tenant_id": str(tenant_id), **ticket_result})


@router.post(
    "/tenant/{tenant_id}/{crm_type}/full-sync",
    summary="Full sync for one tenant (any CRM) — entities then tickets",
)
async def sync_full_for_tenant(
    tenant_id: uuid.UUID,
    crm_type: str,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)

    try:
        registry.get_entry(crm_type)
    except AdapterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown CRM type '{crm_type}'. Available: {registry.list_adapter_keys()}",
        )

    tss = await _get_tss_or_404(tenant_id, crm_type, db)

    entity_result = await _sync_entities_via_adapter(
        tenant_id, crm_type, tss, factory, db
    )
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, crm_type, tss, factory, registry, db
    )

    return success(f"{crm_type} full sync completed", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": tss.crm_org_id,
        "entities":   entity_result,
        "tickets":    ticket_result,
    })


# ===========================================================================
# LEGACY endpoints — kept for backward compatibility
# Internally delegate to the generic adapter path above.
# ===========================================================================

@router.post(
    "/tenant/{tenant_id}/zammad/sync-entities",
    summary="[Legacy] Sync Zammad agents, customers, company for one tenant",
)
async def sync_zammad_entities_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "zammad", db)
    entity_result = await _sync_entities_via_adapter(
        tenant_id, "zammad", tss, factory, db
    )
    return success("Zammad entities synced", {"tenant_id": str(tenant_id), **entity_result})


@router.post(
    "/tenant/{tenant_id}/zammad/sync-tickets",
    summary="[Legacy] Sync Zammad tickets for one tenant",
)
async def sync_zammad_tickets_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "zammad", db)
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, "zammad", tss, factory, registry, db
    )
    return success("Zammad ticket sync completed", {"tenant_id": str(tenant_id), **ticket_result})


@router.post(
    "/tenant/{tenant_id}/zammad/full-sync",
    summary="[Legacy] Full Zammad sync for one tenant — entities then tickets",
)
async def sync_zammad_full_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "zammad", db)
    entity_result = await _sync_entities_via_adapter(
        tenant_id, "zammad", tss, factory, db
    )
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, "zammad", tss, factory, registry, db
    )
    return success("Zammad full sync completed", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": tss.crm_org_id,
        "entities":   entity_result,
        "tickets":    ticket_result,
    })


@router.post(
    "/tenant/{tenant_id}/espocrm/sync-entities",
    summary="[Legacy] Sync EspoCRM agents, customers, company for one tenant",
)
async def sync_espocrm_entities_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "espocrm", db)
    entity_result = await _sync_entities_via_adapter(
        tenant_id, "espocrm", tss, factory, db
    )
    return success("EspoCRM entities synced", {"tenant_id": str(tenant_id), **entity_result})


@router.post(
    "/tenant/{tenant_id}/espocrm/sync-tickets",
    summary="[Legacy] Sync EspoCRM tickets for one tenant",
)
async def sync_espocrm_tickets_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "espocrm", db)
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, "espocrm", tss, factory, registry, db
    )
    return success("EspoCRM ticket sync completed", {"tenant_id": str(tenant_id), **ticket_result})


@router.post(
    "/tenant/{tenant_id}/espocrm/full-sync",
    summary="[Legacy] Full EspoCRM sync for one tenant — entities then tickets",
)
async def sync_espocrm_full_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    await _get_tenant_or_404(tenant_id, db)
    tss = await _get_tss_or_404(tenant_id, "espocrm", db)
    entity_result = await _sync_entities_via_adapter(
        tenant_id, "espocrm", tss, factory, db
    )
    ticket_result = await _sync_tickets_via_adapter(
        tenant_id, "espocrm", tss, factory, registry, db
    )
    return success("EspoCRM full sync completed", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": tss.crm_org_id,
        "entities":   entity_result,
        "tickets":    ticket_result,
    })


# ---------------------------------------------------------------------------
# All-tenant legacy endpoints  (unchanged behaviour)
# ---------------------------------------------------------------------------

@router.post("/zammad/full-sync", summary="Full Zammad sync — all tenants")
async def sync_zammad_full(db: AsyncSession = Depends(get_db)):
    try:
        result = await run_zammad_full_sync(db=None)
    except Exception as exc:
        raise _adapter_error_to_http(exc)
    return success("Zammad full sync completed (all tenants)", result)


@router.post("/espocrm/full-sync", summary="Full EspoCRM sync — all tenants")
async def sync_espocrm_full(db: AsyncSession = Depends(get_db)):
    try:
        result = await run_espocrm_full_sync(db=None)
    except Exception as exc:
        raise _adapter_error_to_http(exc)
    return success("EspoCRM full sync completed (all tenants)", result)


# ---------------------------------------------------------------------------
# POST /sync/{ticket_id}/comments/sync  (unchanged)
# ---------------------------------------------------------------------------

@router.post(
    "/{ticket_id}/comments/sync",
    summary="Sync comments for a ticket from its CRM",
    description=(
        "Fetches comments from the ticket's source CRM, "
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
