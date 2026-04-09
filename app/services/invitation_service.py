"""
app/services/invitation_service.py

Business logic for Invitation operations:
  - validate_invite
  - accept_invite
  - invite_agent
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.keycloak_admin import _get_admin_token, create_keycloak_user
from app.core.settings import get_settings
from app.models.dashboard_user import DashboardUser
from app.models.invitation import Invitation
from app.models.tenant import Tenant
from app.models.user_agent_mapping import UserAgentMapping
from app.models.agent import Agent

settings = get_settings()
logger = logging.getLogger(__name__)


async def svc_validate_invite(db: AsyncSession, token: str) -> dict:
    """
    Validate an invite token and return invite metadata.
    Raises 404 if not found, 410 if already accepted or expired.
    """
    result = await db.execute(
        select(Invitation, Tenant)
        .join(Tenant, Tenant.id == Invitation.tenant_id)
        .where(Invitation.token == token)
    )
    row = result.first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite link not found")

    invite, tenant = row
    if invite.status == "accepted":
        raise HTTPException(
            status.HTTP_410_GONE, "This invite link has already been used"
        )
    if invite.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status.HTTP_410_GONE, "This invite link has expired")

    return {
        "email": invite.email,
        "role": invite.role,
        "tenant_name": tenant.name,
        "tenant_id": str(tenant.id),
        "realm_name": invite.realm_name,
    }


async def svc_accept_invite(db: AsyncSession, token: str, password: str) -> dict:
    """
    Accept an invite: set password in Keycloak, mark email verified,
    create DashboardUser row if not already present, mark invite accepted.
    """
    result = await db.execute(
        select(Invitation, Tenant)
        .join(Tenant, Tenant.id == Invitation.tenant_id)
        .where(Invitation.token == token)
    )
    row = result.first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid invite token")

    invite, tenant = row
    if invite.status == "accepted":
        raise HTTPException(status.HTTP_410_GONE, "Invite already used")
    if invite.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status.HTTP_410_GONE, "Invite expired")

    invite.status = "accepted"
    await db.flush()

    admin_token = await _get_admin_token(invite.realm_name)
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{invite.realm_name}"

    async with httpx.AsyncClient() as client:
        users_resp = await client.get(
            f"{admin_base}/users",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"email": invite.email, "exact": "true"},
        )
        users_resp.raise_for_status()
        users = users_resp.json()
        if not users:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Keycloak user not found")

        kc_user = users[0]
        keycloak_sub = kc_user["id"]

        pwd_resp = await client.put(
            f"{admin_base}/users/{keycloak_sub}/reset-password",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            },
            json={"type": "password", "value": password, "temporary": False},
        )
        if pwd_resp.status_code == 400:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Password does not meet requirements"
            )
        pwd_resp.raise_for_status()

        await client.put(
            f"{admin_base}/users/{keycloak_sub}",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            },
            json={"emailVerified": True},
        )

    existing = await db.execute(
        select(DashboardUser).where(DashboardUser.keycloak_sub == keycloak_sub)
    )
    if not existing.scalars().first():
        full_name = (
            f"{kc_user.get('firstName', '')} {kc_user.get('lastName', '')}".strip()
        )
        db_user = DashboardUser(
            tenant_id=invite.tenant_id,
            keycloak_sub=keycloak_sub,
            name=full_name,
            email=invite.email,
            role=invite.role,
            is_active=True,
        )
        db.add(db_user)
        await db.flush()  # flush so db_user.id is populated

        # Only create agent mappings for agent-role users
        if invite.role == "agent":
            agents_result = await db.execute(
                select(Agent).where(
                    Agent.email     == invite.email,
                    Agent.tenant_id == invite.tenant_id,
                )
            )
            matched_agents = agents_result.scalars().all()

            for agent in matched_agents:
                db.add(UserAgentMapping(
                    dashboard_user_id = db_user.id,
                    agent_id          = agent.id,
                    source_system_id  = agent.source_system_id,
                ))

            if not matched_agents:
                logger.warning(
                    "No agents found for email=%s tenant=%s — user_agent_mapping skipped",
                    invite.email, invite.tenant_id,
                )

    await db.commit()
    return {
        "message": "Account activated",
        "email": invite.email,
        "tenant_name": tenant.name,
    }


async def svc_invite_agent(
    db: AsyncSession,
    current_user: CurrentUser,
    email: str,
    role: str,
    first_name: str,
    last_name: str,
) -> dict:
    """
    Invite an agent to the current admin's tenant.
    Raises 404 if tenant not found, 409 if active invite exists,
    502 on Keycloak failure.
    """
    tenant_id_str = current_user.require_tenant()
    t_uuid = uuid.UUID(tenant_id_str)

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == t_uuid))
    tenant = tenant_result.scalars().first()
    if not tenant:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Tenant record not found in database."
        )

    existing_invite = await db.execute(
        select(Invitation)
        .where(Invitation.email == email)
        .where(Invitation.tenant_id == t_uuid)
        .where(Invitation.status == "pending")
        .where(Invitation.expires_at > datetime.utcnow())
    )
    if existing_invite.scalars().first():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An active invite already exists."
        )

    try:
        await create_keycloak_user(
            email=email,
            first_name=first_name,
            last_name=last_name,
            tenant_id=tenant_id_str,
            role=role,
            realm=settings.KEYCLOAK_REALM,
        )
    except Exception as e:
        logger.error(f"Keycloak error: {e}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Failed to create Keycloak user."
        )

    invite_token = secrets.token_urlsafe(32)
    invitation = Invitation(
        tenant_id=t_uuid,
        email=email,
        role=role,
        token=invite_token,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=24),
        realm_name=settings.KEYCLOAK_REALM,
    )
    db.add(invitation)
    await db.commit()

    return {
        "message": f"Invitation sent to {email}",
        "invite_link": f"{settings.FRONTEND_URL}/invite?token={invite_token}",
        "role": role,
    }