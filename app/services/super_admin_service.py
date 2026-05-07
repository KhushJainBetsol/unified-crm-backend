"""
app/services/super_admin_service.py

Business logic for Super Admin operations:
  - list_source_systems
  - create_tenant
  - invite_admin
  - list_tenants
  - update_tenant
  - delete_tenant
  - list_admins
  - update_admin
  - delete_admin
  - list_all_users
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update as sa_update, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keycloak_admin import (
    create_keycloak_user,
    delete_keycloak_user,
    update_keycloak_user,
)
from app.core.settings import get_settings
from app.models.crm_integration import CrmIntegration
from app.models.dashboard_user import DashboardUser
from app.models.invitation import Invitation
from app.models.source_system import SourceSystem
from app.models.tenant import Tenant
from app.models.tenant_realm import TenantRealm
from app.models.tenant_source_systems import TenantSourceSystem
from app.models.ticket import Ticket
from app.utils.email import send_invite_email

settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source systems
# ---------------------------------------------------------------------------

async def svc_list_source_systems(db: AsyncSession) -> list[dict]:
    """Return all supported source systems."""
    result = await db.execute(select(SourceSystem))
    return [
        {"id": s.id, "system_name": s.system_name}
        for s in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Tenants — create
# ---------------------------------------------------------------------------

async def svc_create_tenant(
    db: AsyncSession,
    name: str,
    contact_email: str,
    key_manager,
) -> dict:
    """
    Create a new tenant.

    Steps:
      1. Generate URL-friendly slug from the tenant name.
      2. Check slug uniqueness.
      3. Insert tenant row.
      4. Seed shared TenantRealm row (idempotent).
      5. Commit.
      6. Generate + store per-tenant versioned AES key in Infisical.
         generate_and_store_tenant_key returns (version, raw_key).
         The version is logged; neither the key nor version is stored
         in the DB at the tenant level.
      7. Return the new tenant.
    """
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
        tenant = Tenant(name=name, slug=slug, contact_email=contact_email)
        db.add(tenant)
        await db.flush()

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

        # Step 5 — commit
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

    # Step 6 — generate + store per-tenant versioned AES key in Infisical
    try:
        key_version, _ = await key_manager.generate_and_store_tenant_key(str(tenant.id))
        logger.info(
            "Per-tenant encryption key stored in Infisical for tenant_id='%s' (version=%s).",
            tenant.id,
            key_version,
        )
    except Exception as exc:
        logger.error(
            "CRITICAL: Tenant '%s' (id=%s) was created in DB but per-tenant "
            "key generation in Infisical FAILED: %s. "
            "The tenant cannot provision integrations until a key is stored "
            "manually as TENANT_KEY_%s_v1 in Infisical.",
            slug, tenant.id, exc, tenant.id,
        )
        return {
            "tenant": {
                "id": str(tenant.id),
                "name": tenant.name,
                "slug": tenant.slug,
                "contact_email": tenant.contact_email,
                "is_active": tenant.is_active,
                "created_at": tenant.created_at,
            },
            "warning": (
                f"Tenant created but encryption key could not be stored in Infisical: {exc}. "
                f"Manually add secret TENANT_KEY_{tenant.id}_v1 and "
                f"TENANT_ACTIVE_VERSION_{tenant.id}=v1 before this tenant provisions integrations."
            ),
            "message": (
                f"Tenant '{tenant.name}' created. "
                "Use POST /super-admin/admins/invite to invite an admin."
            ),
        }

    logger.info("Created tenant '%s' (id=%s).", slug, tenant.id)
    return {
        "tenant": {
            "id": str(tenant.id),
            "name": tenant.name,
            "slug": tenant.slug,
            "contact_email": tenant.contact_email,
            "is_active": tenant.is_active,
            "created_at": tenant.created_at,
        },
        "message": (
            f"Tenant '{tenant.name}' created. "
            "Use POST /super-admin/admins/invite to invite an admin."
        ),
    }


# ---------------------------------------------------------------------------
# Tenants — update
# ---------------------------------------------------------------------------

async def svc_update_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str | None = None,
    contact_email: str | None = None,
    is_active: bool | None = None,
) -> dict:
    """
    Patch tenant metadata.

    When is_active is toggled, every DashboardUser belonging to this tenant
    is simultaneously enabled or disabled in Keycloak so logins are affected
    immediately without waiting for token expiry.

    Keycloak sync errors are collected and returned as a warning rather than
    rolling back the DB change — partial Keycloak sync is recoverable, but
    rolling back the DB would leave the UI in an inconsistent state.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalars().first()
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Tenant '{tenant_id}' not found")

    if name is not None:
        tenant.name = name
    if contact_email is not None:
        tenant.contact_email = contact_email

    kc_errors: list[str] = []

    if is_active is not None and is_active != tenant.is_active:
        tenant.is_active = is_active

        # Propagate enable/disable to every user in Keycloak
        users_result = await db.execute(
            select(DashboardUser).where(DashboardUser.tenant_id == tenant_id)
        )
        users = users_result.scalars().all()

        for u in users:
            if not u.keycloak_sub:
                continue
            try:
                await update_keycloak_user(
                    u.keycloak_sub,
                    settings.KEYCLOAK_REALM,
                    enabled=is_active,
                )
            except Exception as exc:
                kc_errors.append(f"{u.email}: {exc}")

        if kc_errors:
            logger.error(
                "Tenant %s toggled is_active=%s but Keycloak sync failed for: %s",
                tenant_id, is_active, kc_errors,
            )

    try:
        await db.commit()
        await db.refresh(tenant)
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to update tenant %s: %s", tenant_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Tenant update failed due to an internal error.",
        ) from exc

    response: dict = {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "contact_email": tenant.contact_email,
        "is_active": tenant.is_active,
        "updated_at": tenant.updated_at,
    }

    if kc_errors:
        response["warning"] = (
            "Tenant updated in DB but Keycloak sync failed for some users — "
            "their login state may not reflect the new is_active value until "
            f"their tokens expire. Affected accounts: {kc_errors}"
        )

    return response


# ---------------------------------------------------------------------------
# Tenants — delete
# ---------------------------------------------------------------------------

async def svc_delete_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict:
    """
    Permanently remove a tenant and every record that belongs to it.

    Cascade order (FK-safe)
    -----------------------
    1. Soft-delete all tickets        — preserves audit trail, history stays queryable
    2. Hard-delete CRM integrations   — credential references have no value once tenant is gone
    3. Revoke all pending invitations — prevents sign-up links from being used after deletion
    4. Hard-delete TenantSourceSystem — junction table, no audit value
    5. Hard-delete the Tenant row     — Postgres CASCADE removes all DashboardUser rows
                                        (keycloak_subs captured before this commit)
    6. Delete each user from Keycloak — post-commit, best-effort, not in the DB transaction

    Why Keycloak runs after the commit
    ------------------------------------
    Keycloak is outside the Postgres transaction boundary.  If we delete from
    Keycloak first and the DB commit fails, users are locked out but their
    records still exist — harder to recover.  The reverse leaves zombie Keycloak
    accounts for ~5 min (default token TTL) but the DB is consistent.

    Partial Keycloak failures are surfaced in the API response as a warning
    with the affected subs listed for manual cleanup.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalars().first()
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Tenant '{tenant_id}' not found")

    # Capture Keycloak subs BEFORE the commit.
    # The CASCADE on tenants.id will hard-delete dashboard_users rows when the
    # tenant row is removed, so we must read them now.
    subs_result = await db.execute(
        select(DashboardUser.keycloak_sub, DashboardUser.email)
        .where(DashboardUser.tenant_id == tenant_id)
    )
    user_subs: list[tuple[str, str]] = [
        (row.keycloak_sub, row.email)
        for row in subs_result.all()
        if row.keycloak_sub
    ]

    now = datetime.utcnow()

    try:
        # Step 1 — soft-delete all tenant tickets
        await db.execute(
            sa_update(Ticket)
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.is_deleted == False,  # noqa: E712
            )
            .values(
                is_deleted=True,
                deleted_at=now,
                is_deleted_by_crm=False,
            )
        )

        # Step 2 — hard-delete CRM integrations
        await db.execute(
            sa_delete(CrmIntegration).where(CrmIntegration.tenant_id == tenant_id)
        )

        # Step 3 — revoke all pending invitations so sign-up links stop working
        await db.execute(
            sa_update(Invitation)
            .where(Invitation.tenant_id == tenant_id)
            .values(status="revoked", expires_at=now)
        )

        # Step 4 — hard-delete junction rows
        await db.execute(
            sa_delete(TenantSourceSystem).where(TenantSourceSystem.tenant_id == tenant_id)
        )

        # Step 5 — delete the tenant row.
        # Postgres CASCADE fires here and removes all dashboard_users rows
        # whose tenant_id matches.  That is intentional — we captured the
        # keycloak_subs above so we can clean Keycloak up afterwards.
        await db.delete(tenant)
        await db.commit()

    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to delete tenant %s: %s", tenant_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Tenant deletion failed due to an internal error.",
        ) from exc

    logger.info(
        "Tenant %s fully deleted from DB (%d users will be removed from Keycloak).",
        tenant_id, len(user_subs),
    )

    # Step 6 — remove users from Keycloak post-commit (best-effort, not transactional)
    kc_errors: list[str] = []
    for keycloak_sub, email in user_subs:
        try:
            await delete_keycloak_user(keycloak_sub, settings.KEYCLOAK_REALM)
            logger.debug("Deleted Keycloak user %s (%s).", email, keycloak_sub)
        except Exception as exc:
            logger.error(
                "Keycloak delete failed for user %s (sub=%s): %s", email, keycloak_sub, exc
            )
            kc_errors.append(f"{email} (sub={keycloak_sub}): {exc}")

    if kc_errors:
        return {
            "deleted": True,
            "tenant_id": str(tenant_id),
            "warning": (
                "Tenant and all DB records have been deleted, but Keycloak cleanup "
                "failed for some user accounts. Their existing tokens will stop working "
                f"once they expire (typically ~5 minutes). "
                f"Delete the following subs manually in the Keycloak admin console: {kc_errors}"
            ),
        }

    logger.info("Tenant %s fully deleted (DB + Keycloak).", tenant_id)
    return {"deleted": True, "tenant_id": str(tenant_id)}


# ---------------------------------------------------------------------------
# Admins — invite
# ---------------------------------------------------------------------------

async def svc_invite_admin(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    admin_email: str,
    admin_name: str,
) -> dict:
    """
    Invite an admin to an existing tenant.

    Creates the Keycloak user immediately (so the account exists) and sends
    an invitation email with a one-time sign-in link.  The invitation row
    expires after 24 hours.
    """
    try:
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = tenant_result.scalars().first()
        if not tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Tenant '{tenant_id}' not found")
        if not tenant.is_active:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Tenant '{tenant.name}' is not active",
            )

        # Block duplicate active invites
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

        name_parts = admin_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        try:
            keycloak_sub = await create_keycloak_user(
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

        # Persist the keycloak_sub on dashboard_users if a row was pre-created
        # (some flows create the DashboardUser row at invite time rather than
        # at first login — update it here if it exists)
        existing_user = await db.execute(
            select(DashboardUser)
            .where(DashboardUser.email == admin_email)
            .where(DashboardUser.tenant_id == tenant.id)
        )
        user_row = existing_user.scalars().first()
        if user_row and not user_row.keycloak_sub:
            user_row.keycloak_sub = keycloak_sub

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
    logger.info("Admin invite → '%s' for tenant '%s'.", admin_email, tenant.slug)

    await send_invite_email(
        to_email=admin_email,
        invite_link=invite_link,
        tenant_name=tenant.name,
        role="admin",
    )

    return {
        "tenant": {"id": str(tenant.id), "name": tenant.name, "slug": tenant.slug},
        "admin_email": admin_email,
        "invite_link": invite_link,
        "message": f"Invitation sent to {admin_email}. Link expires in 24 hours.",
    }


# ---------------------------------------------------------------------------
# Admins — update
# ---------------------------------------------------------------------------

async def svc_update_admin(
    db: AsyncSession,
    admin_id: uuid.UUID,
    *,
    name: str | None = None,
    email: str | None = None,
    is_active: bool | None = None,
) -> dict:
    """
    Update an admin's profile.

    DB is committed first.  Keycloak is synced immediately after — if it
    fails the DB change is kept (consistent source of truth) and a warning
    is returned so the caller knows to retry the Keycloak sync manually.

    is_active=False disables the Keycloak account immediately, so the user
    cannot obtain new tokens.  Existing tokens expire after their normal TTL
    (~5 min default).
    """
    result = await db.execute(
        select(DashboardUser).where(
            DashboardUser.id == admin_id,
            DashboardUser.role == "admin",
        )
    )
    admin = result.scalars().first()
    if not admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Admin '{admin_id}' not found")

    if name is not None:
        admin.name = name
    if email is not None:
        admin.email = email
    if is_active is not None:
        admin.is_active = is_active
        if not is_active and admin.deleted_at is None:
            admin.deleted_at = datetime.utcnow()
        elif is_active:
            admin.deleted_at = None     # re-activating clears the deleted_at

    try:
        await db.commit()
        await db.refresh(admin)
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to update admin %s: %s", admin_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Admin update failed due to an internal error.",
        ) from exc

    # Sync to Keycloak — best-effort after the DB commit
    kc_warning: str | None = None
    if admin.keycloak_sub:
        name_parts = (admin.name or "").strip().split(" ", 1)
        try:
            await update_keycloak_user(
                admin.keycloak_sub,
                settings.KEYCLOAK_REALM,
                first_name=name_parts[0] if name is not None else None,
                last_name=name_parts[1] if name is not None and len(name_parts) > 1 else None,
                email=email,
                enabled=is_active,
            )
        except Exception as exc:
            logger.error("Keycloak sync failed for admin %s: %s", admin_id, exc)
            kc_warning = (
                "Admin updated in DB but Keycloak sync failed — profile changes and "
                "login state may not take effect until the next token refresh."
            )

    response: dict = {
        "id": str(admin.id),
        "name": admin.name,
        "email": admin.email,
        "is_active": admin.is_active,
        "updated_at": admin.updated_at,
    }
    if kc_warning:
        response["warning"] = kc_warning
    return response


# ---------------------------------------------------------------------------
# Admins — delete
# ---------------------------------------------------------------------------

async def svc_delete_admin(
    db: AsyncSession,
    admin_id: uuid.UUID,
) -> dict:
    """
    Remove an admin account.

    Ticket cascade
    --------------
    Tickets assigned to this admin have agent_id set to NULL.  They remain
    visible, searchable, and re-assignable.  Hard-deleting them would destroy
    customer history — unacceptable.

    DashboardUser row
    -----------------
    Soft-deleted (is_active=False, deleted_at=now) rather than hard-deleted.
    Foreign keys from tickets, comments, and audit records all point to
    dashboard_users.id — a hard delete would require cascading or nullifying
    all of those, both of which destroy audit trails.

    Keycloak
    --------
    The Keycloak account is hard-deleted AFTER the DB commit so the user
    cannot obtain new tokens.  Existing tokens expire at their normal TTL.
    """
    result = await db.execute(
        select(DashboardUser).where(
            DashboardUser.id == admin_id,
            DashboardUser.role == "admin",
        )
    )
    admin = result.scalars().first()
    if not admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Admin '{admin_id}' not found")

    # Capture sub before the flush
    keycloak_sub = admin.keycloak_sub
    now = datetime.utcnow()

    try:
        # Un-assign tickets — preserve history, just remove the agent reference
        await db.execute(
            sa_update(Ticket)
            .where(
                Ticket.agent_id == admin_id,
                Ticket.is_deleted == False,  # noqa: E712
            )
            .values(agent_id=None)
        )

        # Expire any outstanding invitations tied to this email
        await db.execute(
            sa_update(Invitation)
            .where(
                Invitation.email == admin.email,
                Invitation.status == "pending",
            )
            .values(status="revoked", expires_at=now)
        )

        # Soft-delete the DashboardUser row
        admin.is_active = False
        admin.deleted_at = now
        await db.commit()

    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to delete admin %s: %s", admin_id, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Admin deletion failed due to an internal error.",
        ) from exc

    logger.info("Admin %s soft-deleted from DB.", admin_id)

    # Hard-delete from Keycloak post-commit (best-effort)
    if keycloak_sub:
        try:
            await delete_keycloak_user(keycloak_sub, settings.KEYCLOAK_REALM)
            logger.info("Deleted Keycloak user for admin %s (sub=%s).", admin_id, keycloak_sub)
        except Exception as exc:
            logger.error(
                "Keycloak delete failed for admin %s (sub=%s): %s",
                admin_id, keycloak_sub, exc,
            )
            return {
                "deleted": True,
                "admin_id": str(admin_id),
                "warning": (
                    "Admin deactivated in DB and tickets un-assigned, but Keycloak delete "
                    "failed.  Existing tokens will expire at their normal TTL (~5 min).  "
                    f"Delete keycloak_sub='{keycloak_sub}' manually if immediate revocation "
                    "is required."
                ),
            }

    return {"deleted": True, "admin_id": str(admin_id)}


# ---------------------------------------------------------------------------
# Read-only list endpoints
# ---------------------------------------------------------------------------

async def svc_list_tenants(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(Tenant))
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "slug": t.slug,
            "contact_email": t.contact_email,
            "is_active": t.is_active,
            "created_at": t.created_at,
        }
        for t in result.scalars().all()
    ]


async def svc_list_admins(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(DashboardUser, Tenant.name.label("tenant_name"))
        .join(Tenant, Tenant.id == DashboardUser.tenant_id, isouter=True)
        .where(DashboardUser.role == "admin")
    )
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "email": a.email,
            "role": a.role,
            "tenant_id": str(a.tenant_id),
            "tenant_name": tenant_name or "—",
            "is_active": a.is_active,
            "deleted_at": a.deleted_at,
            "created_at": a.created_at,
        }
        for a, tenant_name in result.all()
    ]


async def svc_list_all_users(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(DashboardUser))
    return [
        {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "tenant_id": str(u.tenant_id),
            "is_active": u.is_active,
            "deleted_at": u.deleted_at,
        }
        for u in result.scalars().all()
    ]