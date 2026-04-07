"""
app/services/super_admin_service.py

Business logic for Super Admin operations:
  - list_source_systems
  - create_tenant
  - invite_admin
  - list_tenants
  - list_admins
  - list_all_users
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keycloak_admin import create_keycloak_user
from app.core.settings import get_settings
from app.models.dashboard_user import DashboardUser
from app.models.invitation import Invitation
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_realm import TenantRealm
from app.models.tenant_source_systems import TenantSourceSystem

settings = get_settings()
logger = logging.getLogger(__name__)


async def svc_list_source_systems(db: AsyncSession) -> list[dict]:
    """Return all supported source systems."""
    result = await db.execute(select(SourceSystem))
    return [
        {"id": s.id, "system_name": s.system_name}
        for s in result.scalars().all()
    ]


async def svc_create_tenant(
    db: AsyncSession,
    name: str,
    contact_email: str,
    source_system_ids: List[int],
) -> dict:
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
    if not source_system_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "At least one source system must be selected",
        )

    try:
        # Step 1 — slug
        slug = name.lower().replace(" ", "-").replace("_", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")

        # Step 2 — uniqueness check
        existing = await db.execute(select(Tenant).where(Tenant.slug == slug))
        if existing.scalars().first():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Organisation slug '{slug}' already exists",
            )

        # Step 3 — insert tenant
        tenant = Tenant(name=name, slug=slug)
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
            select(SourceSystem).where(SourceSystem.id.in_(source_system_ids))
        )
        found_systems = ss_result.scalars().all()
        found_ids = {s.id for s in found_systems}
        missing = set(source_system_ids) - found_ids
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


async def svc_invite_admin(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    admin_email: str,
    admin_name: str,
) -> dict:
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
    try:
        # Step 1 — tenant must exist and be active
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = tenant_result.scalars().first()
        if not tenant:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Tenant '{tenant_id}' not found",
            )
        if not tenant.is_active:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Tenant '{tenant.name}' is not active",
            )

        # Step 2 — no duplicate active invite
        dup = await db.execute(
            select(Invitation)
            .where(Invitation.email == admin_email)
            .where(Invitation.tenant_id == tenant.id)
            .where(Invitation.status == "pending")
            .where(Invitation.expires_at > datetime.utcnow())
        )
        if dup.scalars().first():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"An active invite already exists for '{admin_email}' in this tenant",
            )

        # Step 3 — split full name into first / last for Keycloak
        name_parts = admin_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Step 4 — create Keycloak user
        try:
            await create_keycloak_user(
                email=admin_email,
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
            email=admin_email,
            role="admin",
            token=invite_token,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=24),
            realm_name=settings.KEYCLOAK_REALM,
        )
        db.add(invitation)
        await db.commit()

    except HTTPException:
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
    logger.info("Admin invite → '%s' for tenant '%s'", admin_email, tenant.slug)

    return {
        "tenant": {"id": str(tenant.id), "name": tenant.name, "slug": tenant.slug},
        "admin_email": admin_email,
        "invite_link": invite_link,
        "message": (
            f"Invitation sent to {admin_email}. "
            "Link expires in 24 hours."
        ),
    }


async def svc_list_tenants(db: AsyncSession) -> list[dict]:
    """Return all tenants."""
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


async def svc_list_admins(db: AsyncSession) -> list[dict]:
    """Return all admin-role dashboard users across all tenants."""
    result = await db.execute(
        select(DashboardUser).where(DashboardUser.role == "admin")
    )
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "email": a.email,
            "role": a.role,
            "tenant_id": str(a.tenant_id),
            "is_active": a.is_active,
            "created_at": a.created_at,
        }
        for a in result.scalars().all()
    ]


async def svc_list_all_users(db: AsyncSession) -> list[dict]:
    """Return all dashboard users across all tenants."""
    result = await db.execute(select(DashboardUser))
    return [
        {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "tenant_id": str(u.tenant_id),
            "is_active": u.is_active,
        }
        for u in result.scalars().all()
    ]