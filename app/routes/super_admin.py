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

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.settings import get_settings
from app.dependencies import get_db
from app.services.super_admin_service import (
    svc_create_tenant,
    svc_invite_admin,
    svc_list_admins,
    svc_list_all_users,
    svc_list_source_systems,
    svc_list_tenants,
)
from app.credentials.async_manager import AsyncInfisicalCredentialManager

async def get_key_manager(request: Request) -> AsyncInfisicalCredentialManager:
    return request.app.state.key_manager

settings = get_settings()

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

class InviteAdminRequest(BaseModel):
    """Invite an admin to an already-existing tenant."""
    tenant_id: uuid.UUID
    admin_email: EmailStr
    first_name: str
    last_name: str


# ---------------------------------------------------------------------------
# GET /super-admin/source-systems
# ---------------------------------------------------------------------------

@router.get("/source-systems")
async def list_source_systems(
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Return all supported source systems so the frontend can display them."""
    _require_super_admin(user)
    return await svc_list_source_systems(db)


# ---------------------------------------------------------------------------
# POST /super-admin/tenants
# ---------------------------------------------------------------------------

@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,                                          # ← add
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    _require_super_admin(user)
    key_manager: AsyncInfisicalCredentialManager = request.app.state.key_manager   # ← add
    return await svc_create_tenant(
        db=db,
        name=body.name,
        contact_email=body.contact_email,
        key_manager=key_manager,                               # ← add
    )


# ---------------------------------------------------------------------------
# POST /super-admin/admins/invite
# ---------------------------------------------------------------------------

@router.post("/admins/invite", status_code=status.HTTP_201_CREATED)
async def invite_admin(
    body: InviteAdminRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Invite an admin to an already-existing tenant."""
    _require_super_admin(user)
    return await svc_invite_admin(
        db=db,
        tenant_id=body.tenant_id,
        admin_email=body.admin_email,
        admin_name=f"{body.first_name} {body.last_name}".strip(),
    )


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
    return await svc_list_tenants(db)


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
    return await svc_list_admins(db)


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
    return await svc_list_all_users(db)