"""
app/routes/tenants.py

Tenant-scoped endpoints accessible to admin and agent roles only.

  GET   /tenants/me                →  returns the tenant name for the currently logged-in user
  PATCH /tenants/me                →  updates the tenant name/slug for the current tenant
  GET   /tenants/me/source-systems →  returns the CRM systems configured for this tenant
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["Tenants"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class TenantMeResponse(BaseModel):
    id: str
    name: str
    slug: str


class SourceSystemResponse(BaseModel):
    id: int
    system_name: str


class TenantUpdateRequest(BaseModel):
    """
    All fields are optional — only provided fields will be updated (partial update).
    """
    name: str | None = None
    slug: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name must not be blank.")
        return v.strip() if v else v

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str | None) -> str | None:
        """Slugs must be lowercase alphanumeric with hyphens, e.g. 'my-tenant-01'."""
        if v is not None:
            v = v.strip().lower()
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", v):
                raise ValueError(
                    "slug may only contain lowercase letters, digits, and hyphens "
                    "and must not start or end with a hyphen."
                )
        return v


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
        name=row[1].capitalize(),
        slug=row[2],
    )


# ---------------------------------------------------------------------------
# PATCH /tenants/me
# ---------------------------------------------------------------------------


@router.patch("/me", response_model=TenantMeResponse)
async def update_my_tenant(
    payload: TenantUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> TenantMeResponse:
    """
    Partially updates the authenticated user's tenant.
    - Restricted to the **admin** role only (agents may not edit tenant details).
    - At least one field (name or slug) must be provided.
    - Slug uniqueness is enforced at the DB level; a 409 is returned on collision.
    """
    if user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmins are not scoped to a tenant.",
        )

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only tenant admins may edit tenant details.",
        )

    tenant_id = user.require_tenant()

    # Reject empty payloads early — nothing to do.
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field (name, slug) must be provided.",
        )

    # Build SET clause dynamically from whichever fields were supplied.
    set_clauses = ", ".join(f"{col} = :{col}" for col in updates)
    params = {**updates, "tenant_id": tenant_id}

    try:
        result = await db.execute(
            text(
                f"""
                UPDATE tenants
                SET {set_clauses}, updated_at = NOW()
                WHERE id = :tenant_id
                AND is_active = true
                RETURNING id::text, name, slug
                """
            ),
            params,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        # Surface slug uniqueness violations as a clean 409 rather than a 500.
        if "unique" in str(exc).lower() and "slug" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A tenant with that slug already exists.",
            ) from exc
        logger.exception(
            "Failed to update tenant: tenant_id=%s user=%s", tenant_id, user.sub
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the tenant.",
        ) from exc

    row = result.fetchone()
    if not row:
        # Tenant existed at auth time but is gone/inactive now — very unlikely.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found or inactive.",
        )

    logger.info(
        "Tenant updated: tenant_id=%s changes=%s by user=%s",
        tenant_id,
        list(updates.keys()),
        user.sub,
    )

    return TenantMeResponse(
        id=row[0],
        name=row[1].capitalize(),
        slug=row[2],
    )


# ---------------------------------------------------------------------------
# GET /tenants/me/source-systems
# ---------------------------------------------------------------------------


@router.get("/me/source-systems", response_model=list[SourceSystemResponse])
async def get_my_source_systems(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[SourceSystemResponse]:
    """
    Returns the active source CRM systems (e.g., zammad, espocrm)
    linked to the authenticated user's tenant.
    """
    if user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmins are not scoped to a tenant.",
        )

    tenant_id = user.require_tenant()

    result = await db.execute(
        text(
            """
            SELECT ss.id, ss.system_name
            FROM tenant_source_systems tss
            JOIN source_systems ss ON tss.source_system_id = ss.id
            WHERE tss.tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    )

    rows = result.fetchall()

    return [SourceSystemResponse(id=row[0], system_name=row[1]) for row in rows]