"""
Service layer — tenant_source_systems existence check.

Keeps all DB logic out of the router so it stays testable in isolation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_source_systems import TenantSourceSystem
from app.schemas.tenant_source_systems import (
    TenantSourceSystemCheckRequest,
    TenantSourceSystemCheckResponse,
)


async def check_tenant_source_system(
    request: TenantSourceSystemCheckRequest,
    db: AsyncSession,
) -> TenantSourceSystemCheckResponse:
    """
    Check whether a (tenant_id, source_system_id) pair exists in
    tenant_source_systems and return a structured response.

    Args:
        request: Validated request containing tenant_id and source_system_id.
        db:      Async SQLAlchemy session (injected via FastAPI dependency).

    Returns:
        TenantSourceSystemCheckResponse with exists flag, is_active, and message.
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