"""
app/routes/super_admin.py

Super Admin APIs:

  POST /super-admin/tenants              → create tenant in DB + assign source systems
  POST /super-admin/admins/invite        → invite an admin to an existing tenant
  GET  /super-admin/tenants              → list all tenants
  GET  /super-admin/admins               → list all admins (dashboard_users with role=admin)
  GET  /super-admin/users                → list all dashboard users
  GET  /super-admin/source-systems       → list all supported source systems (for frontend dropdown)
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.keycloak_admin import create_keycloak_user
from app.core.settings import get_settings
from app.dependencies import get_db
from app.models.dashboard_user import DashboardUser
from app.models.invitation import Invitation
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_realm import TenantRealm
from app.models.tenant_source_systems import TenantSourceSystem

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/super-admin", tags=["Super Admin"])


def _require_super_admin(user: CurrentUser) -> None:
    """Verify caller is the super admin."""
    if user.email != settings.SUPER_ADMIN_EMAIL and not user.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super admin only")


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateTenantRequest(BaseModel):
    """Tenant info + list of source system IDs the tenant will use."""
    name: str
    contact_email: EmailStr
    source_system_ids: List[int]  # e.g. [1, 2] — must exist in source_systems table


class InviteAdminRequest(BaseModel):
    """Invite an admin to an already-existing tenant."""
    tenant_id: uuid.UUID
    admin_email: EmailStr
    admin_name: str  # full name, split into first/last internally


# ---------------------------------------------------------------------------
# GET /super-admin/source-systems
# Frontend calls this to populate the source systems checklist/dropdown.
# ---------------------------------------------------------------------------

@router.get("/source-systems")
async def list_source_systems(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all supported source systems so the frontend can display them."""
    _require_super_admin(user)
    result = await db.execute(select(SourceSystem))
    return [
        {"id": s.id, "system_name": s.system_name}
        for s in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# POST /super-admin/tenants
# Creates tenant + assigns selected source systems in tenant_source_systems.
# ---------------------------------------------------------------------------

@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Create a new tenant and assign the source systems they use.

    Steps:
      1. Generate URL-friendly slug from tenant name
      2. Check slug uniqueness
      3. Insert tenant row
      4. Seed shared TenantRealm row (idempotent)
      5. Validate all provided source_system_ids exist
      6. Insert a TenantSourceSystem row for each selected source system
      7. Return the new tenant with assigned source systems
    """
    _require_super_admin(user)

    if not body.source_system_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "At least one source system must be selected",
        )

    try:
        # Step 1 — slug
        slug = body.name.lower().replace(" ", "-").replace("_", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")

        # Step 2 — uniqueness check
        existing = await db.execute(select(Tenant).where(Tenant.slug == slug))
        if existing.scalars().first():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Organisation slug '{slug}' already exists",
            )

        # Step 3 — insert tenant
        tenant = Tenant(name=body.name, slug=slug)
        db.add(tenant)
        await db.flush()  # populate tenant.id

        # Step 4 — seed shared realm (idempotent)
        existing_realm = await db.execute(
            select(TenantRealm).where(TenantRealm.realm_name == settings.KEYCLOAK_REALM)
        )
        if not existing_realm.scalars().first():
            realm = TenantRealm(
                tenant_id=None,
                realm_name=settings.KEYCLOAK_REALM,
                issuer_url=f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
                is_active=True,
            )
            db.add(realm)

        # Step 5 — validate all source_system_ids exist in DB
        ss_result = await db.execute(
            select(SourceSystem).where(SourceSystem.id.in_(body.source_system_ids))
        )
        found_systems = ss_result.scalars().all()
        found_ids = {s.id for s in found_systems}
        missing = set(body.source_system_ids) - found_ids
        if missing:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Source system ID(s) not found: {sorted(missing)}",
            )

        # Step 6 — insert TenantSourceSystem rows (one per selected system)
        for ss in found_systems:
            db.add(TenantSourceSystem(
                tenant_id=tenant.id,
                source_system_id=ss.id,
                is_active=True,
            ))

        await db.commit()

    except HTTPException:
        # Roll back any partial writes before bubbling up the HTTP error
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Unexpected error creating tenant: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create tenant due to an internal error.",
        ) from exc

    logger.info(
        "Created tenant '%s' (id=%s) with source systems %s",
        slug, tenant.id, [s.system_name for s in found_systems],
    )

    return {
        "tenant": {
            "id": str(tenant.id),
            "name": tenant.name,
            "slug": tenant.slug,
            "is_active": tenant.is_active,
            "created_at": tenant.created_at,
        },
        "source_systems": [
            {"id": s.id, "system_name": s.system_name} for s in found_systems
        ],
        "message": (
            f"Tenant '{tenant.name}' created with {len(found_systems)} source system(s). "
            "Use POST /super-admin/admins/invite to invite an admin."
        ),
    }


# ---------------------------------------------------------------------------
# POST /super-admin/admins/invite
# Creates Keycloak user + one-time invite token for an existing tenant.
# ---------------------------------------------------------------------------

@router.post("/admins/invite", status_code=status.HTTP_201_CREATED)
async def invite_admin(
    body: InviteAdminRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Invite an admin to an already-existing tenant.

    Steps:
      1. Verify tenant exists and is active
      2. Check no duplicate active (pending + non-expired) invite
      3. Split admin_name into first / last for Keycloak
      4. Create Keycloak user in the shared realm
      5. Generate one-time invite token (24 h expiry) and store it
      6. Return the invite link
    """
    _require_super_admin(user)

    try:
        # Step 1 — tenant must exist and be active
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == body.tenant_id)
        )
        tenant = tenant_result.scalars().first()
        if not tenant:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Tenant '{body.tenant_id}' not found",
            )
        if not tenant.is_active:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Tenant '{tenant.name}' is not active",
            )

        # Step 2 — no duplicate active invite
        dup = await db.execute(
            select(Invitation)
            .where(Invitation.email == body.admin_email)
            .where(Invitation.tenant_id == tenant.id)
            .where(Invitation.status == "pending")
            .where(Invitation.expires_at > datetime.utcnow())
        )
        if dup.scalars().first():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"An active invite already exists for '{body.admin_email}' in this tenant",
            )

        # Step 3 — split full name into first / last for Keycloak
        name_parts = body.admin_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Step 4 — create Keycloak user
        try:
            await create_keycloak_user(
                email=body.admin_email,
                first_name=first_name,
                last_name=last_name,
                tenant_id=str(tenant.id),
                role="admin",
                realm=settings.KEYCLOAK_REALM,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e))
        except Exception as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Keycloak error: {e}")

        # Step 5 — invite token
        invite_token = secrets.token_urlsafe(32)
        invitation = Invitation(
            tenant_id=tenant.id,
            email=body.admin_email,
            role="admin",
            token=invite_token,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=24),
            realm_name=settings.KEYCLOAK_REALM,
        )
        db.add(invitation)
        await db.commit()

    except HTTPException:
        # Roll back any partial writes before bubbling the HTTP error up
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Unexpected error inviting admin: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to invite admin due to an internal error.",
        ) from exc

    invite_link = f"{settings.FRONTEND_URL}/invite?token={invite_token}"
    logger.info("Admin invite → '%s' for tenant '%s'", body.admin_email, tenant.slug)

    return {
        "tenant": {"id": str(tenant.id), "name": tenant.name, "slug": tenant.slug},
        "admin_email": body.admin_email,
        "invite_link": invite_link,
        "message": (
            f"Invitation sent to {body.admin_email}. "
            "Link expires in 24 hours."
        ),
    }


# ---------------------------------------------------------------------------
# GET /super-admin/tenants
# ---------------------------------------------------------------------------

@router.get("/tenants")
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all tenants."""
    _require_super_admin(user)
    result = await db.execute(select(Tenant))
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "slug": t.slug,
            "is_active": t.is_active,
            "created_at": t.created_at,
        }
        for t in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# GET /super-admin/admins
# ---------------------------------------------------------------------------

@router.get("/admins")
async def list_admins(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all admin-role users across all tenants."""
    _require_super_admin(user)
    result = await db.execute(
        select(DashboardUser).where(DashboardUser.role == "admin")
    )
    return [
        {
            "id": str(a.id),
            "name":a.name,
            "email": a.email,
            "role": a.role,
            "tenant_id": str(a.tenant_id),
            "is_active": a.is_active,
            "created_at": a.created_at,
        }
        for a in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# GET /super-admin/users
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_all_users(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all dashboard users across all tenants."""
    _require_super_admin(user)
    result = await db.execute(select(DashboardUser))
    return [
        {
            "id": str(u.id),
            "name":u.name,
            "email": u.email,
            "role": u.role,
            "tenant_id": str(u.tenant_id),
            "is_active": u.is_active,
        }
        for u in result.scalars().all()
    ]