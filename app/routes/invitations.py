"""
app/routes/invitations.py

Invitation flows — exactly matching Flow 1 Part B and Flow 2 from the diagrams.

GET  /invitations/validate       → check token (show "joining ACME Corp as Admin")
POST /invitations/accept         → mark used, set password in Keycloak, create DashboardUser
POST /invitations/invite-agent   → org admin invites an agent (Flow 2 Part A)
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.keycloak_admin import _get_admin_token, create_keycloak_user
from app.core.settings import get_settings
from app.dependencies import get_db
from app.models.dashboard_user import DashboardUser
from app.models.tenant import Tenant
from app.models.invitation import Invitation

settings = get_settings()

router = APIRouter(prefix="/invitations", tags=["Invitations"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ValidateInviteResponse(BaseModel):
    email: str
    role: str
    tenant_name: str
    tenant_id: str
    realm_name: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class InviteAgentRequest(BaseModel):
    email: EmailStr
    role: str = "agent"
    first_name: str = ""
    last_name: str = ""


# ---------------------------------------------------------------------------
# GET /invitations/validate?token=...
# Called when user clicks the invite link — BEFORE showing the set-password page
# Does NOT consume the token
# ---------------------------------------------------------------------------

@router.get("/validate", response_model=ValidateInviteResponse)
async def validate_invite(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Flow 1 Part B step 1 / Flow 2 Part B step 1.

    Returns tenant name + role so the UI can show:
    "You are joining ACME Corp as Admin — set your password"
    Token is NOT marked used here.
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
        raise HTTPException(status.HTTP_410_GONE, "This invite link has already been used")

    if invite.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status.HTTP_410_GONE, "This invite link has expired")

    return ValidateInviteResponse(
        email=invite.email,
        role=invite.role,
        tenant_name=tenant.name,
        tenant_id=str(tenant.id),
        realm_name=invite.realm_name,
    )


# ---------------------------------------------------------------------------
# POST /invitations/accept
# User submits their password — activates the account
# ---------------------------------------------------------------------------

@router.post("/accept")
async def accept_invite(
    body: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Flow 1 Part B / Flow 2 Part B — sets password and activates account.

    Critical order (from diagram):
      1. Validate token (exists, not used, not expired)
      2. Mark token as accepted FIRST — token is dead after this point
         even if the rest fails
      3. Find Keycloak user by email in the realm
      4. Set their password via Keycloak Admin API
      5. Mark email as verified
      6. Create DashboardUser record if not exists
      7. Return success
    """
    result = await db.execute(
        select(Invitation, Tenant)
        .join(Tenant, Tenant.id == Invitation.tenant_id)
        .where(Invitation.token == body.token)
    )
    row = result.first()

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid invite token")

    invite, tenant = row

    if invite.status == "accepted":
        raise HTTPException(status.HTTP_410_GONE, "Invite already used")

    if invite.expires_at.replace(tzinfo=None) < datetime.utcnow():
        raise HTTPException(status.HTTP_410_GONE, "Invite expired")

    # Step 2 — mark as accepted IMMEDIATELY (before any Keycloak call)
    invite.status = "accepted"
    await db.flush()

    # Step 3+4+5 — set password via Keycloak Admin API
    admin_token = await _get_admin_token(invite.realm_name)
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{invite.realm_name}"

    async with httpx.AsyncClient() as client:
        # Find user by email
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

        # Set password
        pwd_resp = await client.put(
            f"{admin_base}/users/{keycloak_sub}/reset-password",
            headers={"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"},
            json={"type": "password", "value": body.password, "temporary": False},
        )
        if pwd_resp.status_code == 400:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Password does not meet requirements (min 8 chars, must include uppercase, number, special char)",
            )
        pwd_resp.raise_for_status()

        # Mark email as verified
        await client.put(
            f"{admin_base}/users/{keycloak_sub}",
            headers={"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"},
            json={"emailVerified": True},
        )

    # Step 6 — create DashboardUser if not already there
    existing = await db.execute(
        select(DashboardUser).where(DashboardUser.keycloak_sub == keycloak_sub)
    )
    if not existing.scalars().first():
        db_user = DashboardUser(
            tenant_id=invite.tenant_id,
            keycloak_sub=keycloak_sub,
            email=invite.email,
            role=invite.role,
            is_active=True,
        )
        db.add(db_user)

    await db.commit()

    return {
        "message": "Account activated successfully",
        "email": invite.email,
        "role": invite.role,
        "tenant_name": tenant.name,
    }


# ---------------------------------------------------------------------------
# POST /invitations/invite-agent
# Org admin (Mike) invites an agent (John) — Flow 2 Part A
# Requires admin JWT
# ---------------------------------------------------------------------------

@router.post("/invite-agent", status_code=status.HTTP_201_CREATED)
async def invite_agent(
    body: InviteAgentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    """
    Flow 2 Part A — Org admin invites an agent.

    Steps:
      1. Verify caller is admin (require_admin dependency)
      2. Get caller's tenant_id from JWT
      3. Check for duplicate active invite
      4. Create Keycloak user (disabled until accept)
      5. Generate one-time invite token (stored in DB)
      6. Return invite link
    """
    tenant_id = current_user.require_tenant()

    # Load tenant record
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == uuid.UUID(tenant_id))
    )
    tenant = tenant_result.scalars().first()
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")

    # Check for existing active invite for this email+tenant
    existing_invite = await db.execute(
        select(Invitation)
        .where(Invitation.email == body.email)
        .where(Invitation.tenant_id == tenant.id)
        .where(Invitation.status == "pending")
        .where(Invitation.expires_at > datetime.utcnow())
    )
    if existing_invite.scalars().first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An active invite already exists for this email",
        )

    # Create Keycloak user
    try:
        await create_keycloak_user(
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            tenant_id=tenant_id,
            role=body.role,
            realm=settings.KEYCLOAK_REALM,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Keycloak error: {e}")

    # Generate invite token
    invite_token = secrets.token_urlsafe(32)
    invitation = Invitation(
        tenant_id=tenant.id,
        email=body.email,
        role=body.role,
        token=invite_token,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=24),
        realm_name=settings.KEYCLOAK_REALM,
    )
    db.add(invitation)
    await db.commit()

    invite_link = f"{settings.FRONTEND_URL}/invite?token={invite_token}"

    return {
        "message": f"Invitation sent to {body.email}",
        "invite_link": invite_link,
        "role": body.role,
    }