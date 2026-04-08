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
from app.models.tenant import Tenant
from app.models.tenant_source_systems import TenantSourceSystem
from app.services.entity_sync_service import EntitySyncService
from app.services.sync_service import SyncService
from app.services.comment_service import CommentService
from app.services.scheduler import (
    run_tenant_full_sync,
    run_zammad_full_sync,
    run_espocrm_full_sync,
    _get_source_system_name,
)
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
    """
    Return the TenantSourceSystem row for (tenant, source_system) or raise 404.
    Also raises 404 if crm_org_id is NULL (org not yet resolved).
    """
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
    """
    Triggers a full sync (entities + tickets) for every active source system
    registered for the given tenant, scoped to their crm_org_id.
    """
    await _get_tenant_or_404(tenant_id, db)
    result = await run_tenant_full_sync(tenant_id, db=None)
    return success(f"Full sync completed for tenant {tenant_id}", result)


# ===========================================================================
# ZAMMAD — tenant-scoped
# ===========================================================================

@router.post(
    "/tenant/{tenant_id}/zammad/sync-entities",
    summary="Sync Zammad agents, customers, company for one tenant",
)
async def sync_zammad_entities_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Step 1 — populate agents, customers, and company for this tenant's Zammad org."""
    await _get_tenant_or_404(tenant_id, db)
    tss              = await _get_tss_or_404(tenant_id, "zammad", db)
    source_system_id = tss.source_system_id
    crm_org_id       = tss.crm_org_id

    try:
        async with ZammadClient() as client:
            raw_agents    = await client.get_agents_by_org(crm_org_id)
            raw_customers = await client.get_customers_by_org(crm_org_id)
            raw_org       = await client.get_organization_by_id(crm_org_id)
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
    customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
    companies_c, companies_u = await svc.sync_zammad_companies([raw_org])

    logger.info(
        "Zammad entity sync tenant=%s org=%s: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
        tenant_id, crm_org_id,
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
    )
    return success("Zammad entities synced", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": crm_org_id,
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    })


@router.post(
    "/tenant/{tenant_id}/zammad/sync-tickets",
    summary="Sync Zammad tickets for one tenant",
)
async def sync_zammad_tickets_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Step 2 — run AFTER sync-entities. Syncs tickets scoped to this tenant's Zammad org."""
    await _get_tenant_or_404(tenant_id, db)
    tss        = await _get_tss_or_404(tenant_id, "zammad", db)
    crm_org_id = tss.crm_org_id

    try:
        async with ZammadClient() as client:
            raw_tickets = await client.get_tickets_by_org(crm_org_id)
            normalized  = ZammadService(client).normalize_raw_tickets(raw_tickets)
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)

    result = await SyncService(db).sync_tickets(
        normalized_tickets = normalized,
        source_system      = "zammad",
        tenant_id          = tenant_id,
    )
    logger.info(
        "Zammad ticket sync tenant=%s org=%s: fetched=%d created=%d updated=%d failed=%d",
        tenant_id, crm_org_id,
        result.total_fetched, result.created, result.updated, result.failed,
    )
    return success("Zammad ticket sync completed", {
        "tenant_id":     str(tenant_id),
        "crm_org_id":    crm_org_id,
        "source_system": result.source_system,
        "total_fetched": result.total_fetched,
        "created":       result.created,
        "updated":       result.updated,
        "failed":        result.failed,
    })


@router.post(
    "/tenant/{tenant_id}/zammad/full-sync",
    summary="Full Zammad sync for one tenant — entities then tickets",
)
async def sync_zammad_full_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Entities + tickets in one call, scoped to this tenant's Zammad org."""
    await _get_tenant_or_404(tenant_id, db)
    tss              = await _get_tss_or_404(tenant_id, "zammad", db)
    source_system_id = tss.source_system_id
    crm_org_id       = tss.crm_org_id

    try:
        async with ZammadClient() as client:
            raw_agents    = await client.get_agents_by_org(crm_org_id)
            raw_customers = await client.get_customers_by_org(crm_org_id)
            raw_org       = await client.get_organization_by_id(crm_org_id)
            raw_tickets   = await client.get_tickets_by_org(crm_org_id)
            normalized    = ZammadService(client).normalize_raw_tickets(raw_tickets)
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_zammad_agents(raw_agents)
    customers_c, customers_u = await svc.sync_zammad_customers(raw_customers)
    companies_c, companies_u = await svc.sync_zammad_companies([raw_org])

    ticket_result = await SyncService(db).sync_tickets(
        normalized_tickets = normalized,
        source_system      = "zammad",
        tenant_id          = tenant_id,
    )

    return success("Zammad full sync completed", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": crm_org_id,
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
    })


# ===========================================================================
# ESPOCRM — tenant-scoped
# ===========================================================================

@router.post(
    "/tenant/{tenant_id}/espocrm/sync-entities",
    summary="Sync EspoCRM agents, customers, company for one tenant",
)
async def sync_espocrm_entities_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Step 1 — populate agents, customers, and company for this tenant's EspoCRM account."""
    await _get_tenant_or_404(tenant_id, db)
    tss              = await _get_tss_or_404(tenant_id, "espocrm", db)
    source_system_id = tss.source_system_id
    crm_org_id       = tss.crm_org_id

    try:
        async with EspoClient() as client:
            raw_agents    = await client.get_agents_by_account(crm_org_id)
            raw_customers = await client.get_contacts_by_account(crm_org_id)
            raw_account   = await client.get_account_by_id(crm_org_id)
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
    customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
    companies_c, companies_u = await svc.sync_espo_companies([raw_account])

    logger.info(
        "EspoCRM entity sync tenant=%s account=%s: agents(%d/%d) customers(%d/%d) companies(%d/%d)",
        tenant_id, crm_org_id,
        agents_c, agents_u, customers_c, customers_u, companies_c, companies_u,
    )
    return success("EspoCRM entities synced", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": crm_org_id,
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    })


@router.post(
    "/tenant/{tenant_id}/espocrm/sync-tickets",
    summary="Sync EspoCRM tickets for one tenant",
)
async def sync_espocrm_tickets_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Step 2 — run AFTER sync-entities. Syncs tickets scoped to this tenant's EspoCRM account."""
    await _get_tenant_or_404(tenant_id, db)
    tss        = await _get_tss_or_404(tenant_id, "espocrm", db)
    crm_org_id = tss.crm_org_id

    try:
        async with EspoClient() as client:
            raw_tickets = await client.get_tickets_by_account(crm_org_id)
            normalized  = EspoService(client).normalize_raw_tickets(raw_tickets)
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)

    result = await SyncService(db).sync_tickets(
        normalized_tickets = normalized,
        source_system      = "espocrm",
        tenant_id          = tenant_id,
    )
    logger.info(
        "EspoCRM ticket sync tenant=%s account=%s: fetched=%d created=%d updated=%d failed=%d",
        tenant_id, crm_org_id,
        result.total_fetched, result.created, result.updated, result.failed,
    )
    return success("EspoCRM ticket sync completed", {
        "tenant_id":     str(tenant_id),
        "crm_org_id":    crm_org_id,
        "source_system": result.source_system,
        "total_fetched": result.total_fetched,
        "created":       result.created,
        "updated":       result.updated,
        "failed":        result.failed,
    })


@router.post(
    "/tenant/{tenant_id}/espocrm/full-sync",
    summary="Full EspoCRM sync for one tenant — entities then tickets",
)
async def sync_espocrm_full_for_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Entities + tickets in one call, scoped to this tenant's EspoCRM account."""
    await _get_tenant_or_404(tenant_id, db)
    tss              = await _get_tss_or_404(tenant_id, "espocrm", db)
    source_system_id = tss.source_system_id
    crm_org_id       = tss.crm_org_id

    try:
        async with EspoClient() as client:
            raw_agents    = await client.get_agents_by_account(crm_org_id)
            raw_customers = await client.get_contacts_by_account(crm_org_id)
            raw_account   = await client.get_account_by_id(crm_org_id)
            raw_tickets   = await client.get_tickets_by_account(crm_org_id)
            normalized    = EspoService(client).normalize_raw_tickets(raw_tickets)
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_espo_agents(raw_agents)
    customers_c, customers_u = await svc.sync_espo_customers(raw_customers)
    companies_c, companies_u = await svc.sync_espo_companies([raw_account])

    ticket_result = await SyncService(db).sync_tickets(
        normalized_tickets = normalized,
        source_system      = "espocrm",
        tenant_id          = tenant_id,
    )

    return success("EspoCRM full sync completed", {
        "tenant_id":  str(tenant_id),
        "crm_org_id": crm_org_id,
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
    })


# ===========================================================================
# LEGACY — kept for backward compat, iterate all tenants
# ===========================================================================

@router.post("/zammad/full-sync", summary="Full Zammad sync — all tenants")
async def sync_zammad_full(db: AsyncSession = Depends(get_db)):
    """Triggers Zammad sync for every tenant that has a Zammad integration."""
    try:
        result = await run_zammad_full_sync(db=None)
    except (ZammadClientError, ZammadAuthError) as exc:
        raise _crm_error_to_http(exc)
    return success("Zammad full sync completed (all tenants)", result)


@router.post("/espocrm/full-sync", summary="Full EspoCRM sync — all tenants")
async def sync_espocrm_full(db: AsyncSession = Depends(get_db)):
    """Triggers EspoCRM sync for every tenant that has an EspoCRM integration."""
    try:
        result = await run_espocrm_full_sync(db=None)
    except (EspoClientError, EspoAuthError) as exc:
        raise _crm_error_to_http(exc)
    return success("EspoCRM full sync completed (all tenants)", result)


# ---------------------------------------------------------------------------
# POST /sync/{ticket_id}/comments/sync
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