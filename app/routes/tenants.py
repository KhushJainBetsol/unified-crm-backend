"""
app/routes/tenants.py

Tenant-scoped endpoints accessible to admin and agent roles only.

  GET   /tenants/me                →  returns the tenant name for the currently logged-in user
  PATCH /tenants/me                →  updates name/slug/contact_email and source systems
  GET   /tenants/me/source-systems →  returns the CRM systems configured for this tenant
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["Tenants"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class SourceSystemResponse(BaseModel):
    id: int
    system_name: str


class TenantMeResponse(BaseModel):
    id: str
    name: str
    slug: str
    contact_email: str | None
    source_systems: list[SourceSystemResponse]


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class TenantUpdateRequest(BaseModel):
    """
    All fields are optional — only provided fields will be updated (partial update).

    - ``name``              Human-readable tenant name.
    - ``slug``              URL-friendly identifier; must be lowercase alphanumeric
                            with internal hyphens (e.g. ``my-tenant-01``).
    - ``contact_email``     Primary contact address for the tenant.
    - ``source_system_ids`` When supplied, **replaces** the full set of linked CRM
                            systems for the tenant.  Existing rows whose IDs are not
                            in the new list are deleted; new IDs are inserted; rows
                            present in both are left untouched (preserving
                            ``crm_org_id`` and ``is_active``).
    """

    name: str | None = None
    slug: str | None = None
    contact_email: EmailStr | None = None
    source_system_ids: list[int] | None = None

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

    @field_validator("source_system_ids")
    @classmethod
    def source_system_ids_not_empty(cls, v: list[int] | None) -> list[int] | None:
        """Passing an empty list is almost certainly a caller mistake."""
        if v is not None and len(v) == 0:
            raise ValueError(
                "source_system_ids must contain at least one ID, "
                "or be omitted entirely to leave source systems unchanged."
            )
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_tenant_with_source_systems(
    db: AsyncSession,
    tenant_id: str,
) -> TenantMeResponse | None:
    """
    Fetches the tenant row together with its linked source systems in one round-trip.
    Returns ``None`` when the tenant does not exist or is inactive.
    """
    result = await db.execute(
        text(
            """
            SELECT
                t.id::text,
                t.name,
                t.slug,
                t.contact_email,
                ss.id          AS ss_id,
                ss.system_name AS ss_name
            FROM tenants t
            LEFT JOIN tenant_source_systems tss ON tss.tenant_id = t.id
            LEFT JOIN source_systems        ss  ON ss.id = tss.source_system_id
            WHERE t.id = :tenant_id
              AND t.is_active = true
            """
        ),
        {"tenant_id": tenant_id},
    )
    rows = result.fetchall()

    if not rows:
        return None

    first = rows[0]
    source_systems = [
        SourceSystemResponse(id=row.ss_id, system_name=row.ss_name)
        for row in rows
        if row.ss_id is not None
    ]

    return TenantMeResponse(
        id=first.id,
        name=first.name.capitalize(),
        slug=first.slug,
        contact_email=first.contact_email,
        source_systems=source_systems,
    )


async def _replace_source_systems(
    db: AsyncSession,
    tenant_id: str,
    new_ids: list[int],
) -> None:
    """
    Replaces the tenant's source-system links inside the *caller's* transaction.

    Strategy (preserves ``crm_org_id`` / ``is_active`` on retained rows):
      1. Validate that every requested ID exists in ``source_systems``.
      2. Delete rows whose ``source_system_id`` is NOT in the new set.
      3. Insert missing rows with ``is_active = true`` (ON CONFLICT DO NOTHING
         keeps existing rows intact).
    """
    # 1. Validate — catch unknown IDs before touching junction rows.
    validation = await db.execute(
        text("SELECT id FROM source_systems WHERE id = ANY(:ids)"),
        {"ids": new_ids},
    )
    found_ids = {row[0] for row in validation.fetchall()}
    unknown = set(new_ids) - found_ids
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown source_system_id(s): {sorted(unknown)}",
        )

    # 2. Remove de-listed systems.
    await db.execute(
        text(
            """
            DELETE FROM tenant_source_systems
            WHERE tenant_id = :tenant_id
              AND source_system_id <> ALL(:ids)
            """
        ),
        {"tenant_id": tenant_id, "ids": new_ids},
    )

    # 3. Insert newly added systems (skip duplicates without touching existing rows).
    await db.execute(
        text(
            """
            INSERT INTO tenant_source_systems (tenant_id, source_system_id, is_active)
            SELECT :tenant_id, unnest(:ids::int[]), true
            ON CONFLICT (tenant_id, source_system_id) DO NOTHING
            """
        ),
        {"tenant_id": tenant_id, "ids": new_ids},
    )


# ---------------------------------------------------------------------------
# GET /tenants/me
# ---------------------------------------------------------------------------


@router.get("/me", response_model=TenantMeResponse)
async def get_my_tenant(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> TenantMeResponse:
    """
    Returns the tenant details (including linked source systems) for the
    currently authenticated user.
    Accessible to admin and agent roles only — superadmin has no tenant.
    """
    if user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmins are not scoped to a tenant.",
        )

    tenant_id = user.require_tenant()

    tenant = await _fetch_tenant_with_source_systems(db, tenant_id)
    if not tenant:
        logger.warning(
            "Tenant not found or inactive: tenant_id=%s sub=%s", tenant_id, user.sub
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found or inactive.",
        )

    return tenant


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

    - Restricted to the **admin** role (agents may not edit tenant details).
    - At least one field must be provided.
    - ``source_system_ids``, when supplied, fully **replaces** the existing set.
    - Slug uniqueness is enforced at the DB level; a 409 is returned on collision.
    - All mutations execute inside a single transaction — either every change
      commits or nothing does.
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

    # Separate scalar tenant fields from the relationship field.
    TENANT_COLUMNS = {"name", "slug", "contact_email"}
    all_updates = payload.model_dump(exclude_none=True)
    scalar_updates = {k: v for k, v in all_updates.items() if k in TENANT_COLUMNS}
    new_source_ids: list[int] | None = all_updates.get("source_system_ids")

    if not scalar_updates and new_source_ids is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field (name, slug, contact_email, source_system_ids) must be provided.",
        )

    try:
        # -- 1. Update scalar tenant columns (when any were supplied) ----------
        if scalar_updates:
            set_clauses = ", ".join(f"{col} = :{col}" for col in scalar_updates)
            params = {**scalar_updates, "tenant_id": tenant_id}

            result = await db.execute(
                text(
                    f"""
                    UPDATE tenants
                    SET {set_clauses}, updated_at = NOW()
                    WHERE id = :tenant_id
                      AND is_active = true
                    RETURNING id
                    """
                ),
                params,
            )
            if not result.fetchone():
                # Tenant was active at auth time but is now gone — very unlikely.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Tenant not found or inactive.",
                )

        # -- 2. Replace source systems (when a new set was supplied) -----------
        if new_source_ids is not None:
            await _replace_source_systems(db, tenant_id, new_source_ids)

        await db.commit()

    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
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

    logger.info(
        "Tenant updated: tenant_id=%s scalar_changes=%s source_systems_replaced=%s by user=%s",
        tenant_id,
        list(scalar_updates.keys()),
        new_source_ids is not None,
        user.sub,
    )

    # Re-fetch so the response always reflects the committed DB state.
    tenant = await _fetch_tenant_with_source_systems(db, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found or inactive.",
        )
    return tenant


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

    return [SourceSystemResponse(id=row[0], system_name=row[1]) for row in result.fetchall()]