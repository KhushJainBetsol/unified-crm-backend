"""
app/routes/tenants.py

Tenant-scoped endpoints accessible to admin and agent roles only.

  GET /tenants/me  →  returns the tenant name for the currently logged-in user
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["Tenants"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class TenantMeResponse(BaseModel):
    id: str
    name: str
    slug: str


# ---------------------------------------------------------------------------
# GET /tenants/me
# ---------------------------------------------------------------------------


@router.get("/me", response_model=TenantMeResponse)
async def get_my_tenant(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> TenantMeResponse:
    """
    Returns the tenant details for the currently authenticated user.
    Accessible to admin and agent roles only — superadmin has no tenant.
    """
    if user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmins are not scoped to a tenant.",
        )

    tenant_id = user.require_tenant()  # raises 403 if missing

    result = await db.execute(
        text(
            """
            SELECT id::text, name, slug
            FROM tenants
            WHERE id = :tenant_id
            AND is_active = true
            LIMIT 1
        """
        ),
        {"tenant_id": tenant_id},
    )
    row = result.fetchone()

    if not row:
        logger.warning(
            "Tenant not found or inactive: tenant_id=%s sub=%s", tenant_id, user.sub
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found or inactive.",
        )

    return TenantMeResponse(
        id=row[0],
        name=row[1].capitalize(),  # "betsol" → "Betsol"
        slug=row[2],
    )
