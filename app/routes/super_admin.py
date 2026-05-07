"""
app/routers/super_admin.py

Super-admin REST endpoints.

All routes require the `superadmin` Keycloak role (enforced via
require_superadmin dependency).

Route map
---------
GET    /super-admin/source-systems            list_source_systems
POST   /super-admin/tenants                  create_tenant
GET    /super-admin/tenants                  list_tenants
PATCH  /super-admin/tenants/{tenant_id}      update_tenant
DELETE /super-admin/tenants/{tenant_id}      delete_tenant
POST   /super-admin/admins/invite            invite_admin
GET    /super-admin/admins                   list_admins
PATCH  /super-admin/admins/{admin_id}        update_admin
DELETE /super-admin/admins/{admin_id}        delete_admin
GET    /super-admin/users                    list_all_users
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_superadmin
from app.dependencies import get_db, get_key_manager
from app.services.super_admin_service import (
    svc_create_tenant,
    svc_delete_admin,
    svc_delete_tenant,
    svc_invite_admin,
    svc_list_admins,
    svc_list_all_users,
    svc_list_source_systems,
    svc_list_tenants,
    svc_update_admin,
    svc_update_tenant,
)

router = APIRouter(
    prefix="/super-admin",
    tags=["Super Admin"],
    dependencies=[Depends(require_superadmin)],
)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, description="Human-readable org name")
    contact_email: EmailStr = Field(..., description="Primary contact email for this tenant")


class UpdateTenantRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    contact_email: EmailStr | None = Field(default=None)
    is_active: bool | None = Field(
        default=None,
        description=(
            "Toggle tenant access.  Setting False immediately disables every "
            "user's Keycloak account so they cannot obtain new tokens."
        ),
    )


class InviteAdminRequest(BaseModel):
    tenant_id: uuid.UUID = Field(..., description="UUID of the tenant to add the admin to")
    admin_email: EmailStr = Field(..., description="Email address of the new admin")
    first_name: str = Field(..., min_length=1, max_length=120)
    last_name: str = Field(..., min_length=1, max_length=120)


class UpdateAdminRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: EmailStr | None = Field(
        default=None,
        description="Changing email also updates the Keycloak username",
    )
    is_active: bool | None = Field(
        default=None,
        description=(
            "Setting False disables the Keycloak account immediately. "
            "Setting True re-enables it and clears the deleted_at timestamp."
        ),
    )


# ---------------------------------------------------------------------------
# Source systems
# ---------------------------------------------------------------------------

@router.get(
    "/source-systems",
    summary="List all supported CRM source systems",
)
async def list_source_systems(db: AsyncSession = Depends(get_db)):
    return await svc_list_source_systems(db)


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

@router.post(
    "/tenants",
    status_code=201,
    summary="Create a new tenant",
)
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    key_manager=Depends(get_key_manager),
):
    return await svc_create_tenant(
        db,
        name=body.name,
        contact_email=str(body.contact_email),
        key_manager=key_manager,
    )


@router.get(
    "/tenants",
    summary="List all tenants",
)
async def list_tenants(db: AsyncSession = Depends(get_db)):
    return await svc_list_tenants(db)


@router.patch(
    "/tenants/{tenant_id}",
    summary="Update tenant metadata",
    description=(
        "Patch any combination of name, contact_email, or is_active.  "
        "Toggling is_active propagates to every user's Keycloak account synchronously."
    ),
)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: UpdateTenantRequest,
    db: AsyncSession = Depends(get_db),
):
    return await svc_update_tenant(
        db,
        tenant_id,
        name=body.name,
        contact_email=str(body.contact_email) if body.contact_email else None,
        is_active=body.is_active,
    )


@router.delete(
    "/tenants/{tenant_id}",
    summary="Permanently delete a tenant",
    description=(
        "Hard-deletes the tenant and cascades: "
        "tickets (soft-deleted) · CRM integrations · invitations · "
        "TenantSourceSystems · DashboardUsers (DB CASCADE + Keycloak hard-delete). "
        "This operation is irreversible — present a confirmation dialog in the UI before calling."
    ),
)
async def delete_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    return await svc_delete_tenant(db, tenant_id)


# ---------------------------------------------------------------------------
# Admins
# ---------------------------------------------------------------------------

@router.post(
    "/admins/invite",
    status_code=201,
    summary="Invite an admin to an existing tenant",
)
async def invite_admin(
    body: InviteAdminRequest,
    db: AsyncSession = Depends(get_db),
):
    return await svc_invite_admin(
        db,
        tenant_id=body.tenant_id,
        admin_email=str(body.admin_email),
        admin_name=f"{body.first_name} {body.last_name}".strip(),
    )


@router.get(
    "/admins",
    summary="List all admins across all tenants",
)
async def list_admins(db: AsyncSession = Depends(get_db)):
    return await svc_list_admins(db)


@router.patch(
    "/admins/{admin_id}",
    summary="Update an admin's profile",
    description=(
        "Patch name, email, or is_active.  "
        "All changes are committed to the DB first, then synced to Keycloak. "
        "If Keycloak sync fails, a warning is returned alongside the updated record."
    ),
)
async def update_admin(
    admin_id: uuid.UUID,
    body: UpdateAdminRequest,
    db: AsyncSession = Depends(get_db),
):
    return await svc_update_admin(
        db,
        admin_id,
        name=body.name,
        email=str(body.email) if body.email else None,
        is_active=body.is_active,
    )


@router.delete(
    "/admins/{admin_id}",
    summary="Delete an admin",
    description=(
        "Soft-deletes the DashboardUser row (preserves audit FK targets), "
        "un-assigns their open tickets (agent_id → NULL), revokes pending invitations, "
        "and hard-deletes the Keycloak account so they cannot log in."
    ),
)
async def delete_admin(
    admin_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    return await svc_delete_admin(db, admin_id)


# ---------------------------------------------------------------------------
# Users (read-only, all roles)
# ---------------------------------------------------------------------------

@router.get(
    "/users",
    summary="List all dashboard users across all tenants",
)
async def list_all_users(db: AsyncSession = Depends(get_db)):
    return await svc_list_all_users(db)