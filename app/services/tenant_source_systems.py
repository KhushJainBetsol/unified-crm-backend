"""
Service layer — tenant_source_systems existence check + active integrations list.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_source_systems import TenantSourceSystem
from app.schemas.tenant_source_systems import (
    TenantSourceSystemCheckRequest,
    TenantSourceSystemCheckResponse,
    TenantActiveIntegrationsResponse,
)


async def check_tenant_source_system(
    request: TenantSourceSystemCheckRequest,
    db: AsyncSession,
) -> TenantSourceSystemCheckResponse:
    """
    Check whether a (tenant_id, source_system_id) pair exists in
    tenant_source_systems and return a structured response.
    """
    stmt = select(TenantSourceSystem).where(
        TenantSourceSystem.tenant_id == request.tenant_id,
        TenantSourceSystem.source_system_id == request.source_system_id,
    )
    result = await db.execute(stmt)
    record: TenantSourceSystem | None = result.scalar_one_or_none()

    if record is None:
        return TenantSourceSystemCheckResponse(
            exists=False,
            is_active=None,
            message=(
                f"No mapping found for tenant_id='{request.tenant_id}' "
                f"and source_system_id={request.source_system_id}."
            ),
            tenant_id=request.tenant_id,
            source_system_id=request.source_system_id,
        )

    return TenantSourceSystemCheckResponse(
        exists=True,
        is_active=record.is_active,
        message=(
            f"Mapping exists for tenant_id='{request.tenant_id}' "
            f"and source_system_id={request.source_system_id}. "
            f"Status: {'active' if record.is_active else 'inactive'}."
        ),
        tenant_id=request.tenant_id,
        source_system_id=request.source_system_id,
    )


async def get_active_integrations(
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> TenantActiveIntegrationsResponse:
    """
    Return all source_system_ids where is_active=True for the given tenant.

    Args:
        tenant_id: UUID of the tenant.
        db:        Async SQLAlchemy session (injected via FastAPI dependency).

    Returns:
        TenantActiveIntegrationsResponse containing the list of active
        source_system_ids and their count.
    """
    stmt = select(TenantSourceSystem.source_system_id).where(
        TenantSourceSystem.tenant_id == tenant_id,
        TenantSourceSystem.is_active.is_(True),
    )
    result = await db.execute(stmt)
    active_ids: list[int] = list(result.scalars().all())

    return TenantActiveIntegrationsResponse(
        tenant_id=tenant_id,
        active_source_system_ids=active_ids,
        count=len(active_ids),
    )