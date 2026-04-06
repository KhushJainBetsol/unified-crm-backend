"""
app/routes/super_admin.py

Flow 1 — Super Admin Onboards Organisation Admin.

POST /super-admin/tenants        → create org + invite first admin
GET  /super-admin/tenants        → list all tenants
GET  /super-admin/users          → list all dashboard users

Super admin is identified by email match (SUPER_ADMIN_EMAIL env var).
Production: replace with dedicated superadmin role check.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.keycloak_admin import create_keycloak_user
from app.core.settings import get_settings
from app.dependencies import get_db
from app.models.dashboard_user import DashboardUser
from app.models.tenant import Tenant
from app.models.invitation import Invitation
from app.models.tenant_realm import TenantRealm

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
    name: str
    admin_email: EmailStr
    admin_first_name: str = "Admin"
    admin_last_name: str = "User"


# ---------------------------------------------------------------------------
# POST /super-admin/tenants
# Flow 1 Part A — Super Admin creates org and invites org admin
# ---------------------------------------------------------------------------

@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Flow 1 — Super Admin creates an organisation and sends first admin invite.

    Steps (from Flow 1 diagram):
      1. Generate slug from org name (ACME Corp → acme-corp)
      2. Create tenant in DB
      3. Seed shared tenant_realms row (unified-crm realm)
      4. Create Keycloak user for org admin in unified-crm realm
      5. Generate one-time invite token (24h expiry)
      6. Return invite link
    """
    _require_super_admin(user)

    # Step 1 — slug
    slug = body.name.lower().replace(" ", "-").replace("_", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")

    existing = await db.execute(select(Tenant).where(Tenant.slug == slug))
    if existing.scalars().first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Organisation slug '{slug}' already exists",
        )

    # Step 2 — tenant
    tenant = Tenant(name=body.name, slug=slug)
    db.add(tenant)
    await db.flush()  # get tenant.id

    # Step 3 — seed shared realm (only once)
    existing_realm = await db.execute(
        select(TenantRealm).where(TenantRealm.realm_name == settings.KEYCLOAK_REALM)
    )
    if not existing_realm.scalars().first():
        realm = TenantRealm(
            tenant_id=None,  # NULL = shared realm
            realm_name=settings.KEYCLOAK_REALM,
            issuer_url=f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
            is_active=True,
        )
        db.add(realm)

    # Step 4 — Keycloak user for org admin
    try:
        await create_keycloak_user(
            email=body.admin_email,
            first_name=body.admin_first_name,
            last_name=body.admin_last_name,
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

    invite_link = f"{settings.FRONTEND_URL}/invite?token={invite_token}"
    logger.info("Created tenant %s, invite sent to %s", slug, body.admin_email)

    return {
        "tenant": {"id": str(tenant.id), "name": tenant.name, "slug": tenant.slug},
        "admin_email": body.admin_email,
        "invite_link": invite_link,
        "message": f"Tenant created. Send this invite link to {body.admin_email}",
    }


# ---------------------------------------------------------------------------
# GET /super-admin/tenants
# ---------------------------------------------------------------------------

@router.get("/tenants")
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """List all active tenants."""
    _require_super_admin(user)
    result = await db.execute(select(Tenant).where(Tenant.is_active == True))  # noqa: E712
    tenants = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "slug": t.slug,
            "is_active": t.is_active,
            "created_at": t.created_at,
        }
        for t in tenants
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
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "role": u.role,
            "tenant_id": str(u.tenant_id),
            "is_active": u.is_active,
        }
        for u in users
    ]