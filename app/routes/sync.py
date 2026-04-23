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

Ticket sync uses the adapter's fetch_tickets() method.  Because the
adapter already normalizes raw CRM JSON into UnifiedTicket objects,
we convert UnifiedTicket → NormalizedTicket directly and skip the
dict-based normalizer pipeline entirely.

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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapter_dependencies.deps import get_adapter_factory, get_adapter_registry
from app.config.registry import AdapterNotFoundError, AdapterRegistry
from app.dependencies import get_db
from app.domain.models import UnifiedTicket
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory
from app.integrations.normalizer.schema import NormalizedTicket
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
# UnifiedTicket → NormalizedTicket converter
# ---------------------------------------------------------------------------

def _unified_to_normalized(ticket: UnifiedTicket, source_system: str) -> NormalizedTicket:
    """
    Convert an adapter-produced UnifiedTicket directly into a NormalizedTicket
    that SyncService.sync_tickets() can consume.

    This bypasses the dict-based normalizer pipeline because the adapter has
    already done the field extraction and status/priority mapping.
    """
    # status: TicketStatus enum → plain string (e.g. "open", "closed")
    status_val = ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)

    # priority: TicketPriority enum → plain string, None for "unknown"
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
        closed_at       = None,  # UnifiedTicket has no closed_at field
    )


# ---------------------------------------------------------------------------
# Shared DB helpers
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

    try:
        adapter = await factory.create(str(tss.integration_id))
        async with adapter:
            agents_result    = await adapter.fetch_agents()
            customers_result = await adapter.fetch_customers()
            orgs_result      = await adapter.fetch_organizations()
    except AdapterFactoryError as exc:
        raise _adapter_error_to_http(exc)
    except Exception as exc:
        raise _adapter_error_to_http(exc)

    # Unwrap PaginatedResult — items are UnifiedAgent/UnifiedCustomer/UnifiedOrganization
    raw_agents    = agents_result.items
    raw_customers = customers_result.items
    raw_orgs      = orgs_result.items

    svc = EntitySyncService(db, source_system_id, tenant_id)
    agents_c,    agents_u    = await svc.sync_agents(raw_agents,    crm_type)
    customers_c, customers_u = await svc.sync_customers(raw_customers, crm_type)
    companies_c, companies_u = await svc.sync_companies(raw_orgs,    crm_type)

    return {
        "agents":    {"created": agents_c,    "updated": agents_u},
        "customers": {"created": customers_c, "updated": customers_u},
        "companies": {"created": companies_c, "updated": companies_u},
    }


async def _sync_tickets_via_adapter(
    tenant_id: uuid.UUID,
    crm_type: str,
    tss: TenantSourceSystem,
    factory: CrmAdapterFactory,
    db: AsyncSession,
) -> dict:
    """
    Fetch tickets from the CRM via the adapter, convert UnifiedTicket →
    NormalizedTicket, and upsert into the DB.  Returns a summary dict.

    Note: the adapter already normalizes raw CRM JSON into UnifiedTicket
    objects, so we convert directly to NormalizedTicket instead of running
    the dict-based normalizer pipeline a second time.
    """
    try:
        adapter = await factory.create(str(tss.integration_id))
        async with adapter:
            tickets_result = await adapter.fetch_tickets()
    except AdapterFactoryError as exc:
        raise _adapter_error_to_http(exc)
    except Exception as exc:
        raise _adapter_error_to_http(exc)

    # Convert UnifiedTicket objects → NormalizedTicket objects directly
    normalized: list[NormalizedTicket] = [
        _unified_to_normalized(t, crm_type)
        for t in tickets_result.items
    ]

    result = await SyncService(db).sync_tickets(
        normalized_tickets = normalized,
        source_system      = crm_type,
        tenant_id          = tenant_id,
    )

    logger.info(
        "%s ticket sync tenant=%s: fetched=%d created=%d updated=%d failed=%d",
        crm_type, tenant_id,
        result.total_fetched, result.created, result.updated, result.failed,
    )

    return {
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

    logger.info("%s entity sync tenant=%s: %s", crm_type, tenant_id, entity_result)
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
        tenant_id, crm_type, tss, factory, db
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
        tenant_id, crm_type, tss, factory, db
    )

    return success(f"{crm_type} full sync completed", {
        "tenant_id": str(tenant_id),
        "entities":  entity_result,
        "tickets":   ticket_result,
    })


# ===========================================================================
# LEGACY endpoints — kept for backward compatibility
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
        tenant_id, "zammad", tss, factory, db
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
        tenant_id, "zammad", tss, factory, db
    )
    return success("Zammad full sync completed", {
        "tenant_id": str(tenant_id),
        "entities":  entity_result,
        "tickets":   ticket_result,
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
        tenant_id, "espocrm", tss, factory, db
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
        tenant_id, "espocrm", tss, factory, db
    )
    return success("EspoCRM full sync completed", {
        "tenant_id": str(tenant_id),
        "entities":  entity_result,
        "tickets":   ticket_result,
    })


# ---------------------------------------------------------------------------
# All-tenant legacy endpoints
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
# POST /sync/{ticket_id}/comments/sync
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