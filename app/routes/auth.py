"""
app/routes/auth.py

Auth utility endpoints consumed by the frontend.

GET  /auth/realm-config?subdomain=acme  → tells frontend which Keycloak realm to use
GET  /auth/me                           → returns current user info from validated JWT

These are the only two endpoints the frontend needs before and after login.
No passwords ever touch this backend.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.settings import get_settings
from app.dependencies import get_db
from app.models.tenant import Tenant
from app.models.tenant_realm import TenantRealm

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ---------------------------------------------------------------------------
# Response schemas (local — no need for separate schema file)
# ---------------------------------------------------------------------------

class RealmConfigResponse(BaseModel):
    realm_name: str
    client_id: str
    issuer_url: str


class MeResponse(BaseModel):
    sub: str
    email: str
    name: str
    roles: list[str]
    tenant_id: str | None
    is_admin: bool
    is_agent: bool
    is_superadmin: bool


# ---------------------------------------------------------------------------
# GET /auth/realm-config
# Called by keycloak.js BEFORE initialising Keycloak
# ---------------------------------------------------------------------------

@router.get("/realm-config", response_model=RealmConfigResponse)
async def get_realm_config(
    subdomain: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns which Keycloak realm the frontend should initialise.

    Single realm phase (now): always returns unified-crm.
    Multi-realm phase (future): subdomain acme → acme-corp realm row in tenant_realms.

    Frontend calls this before keycloak.init() so it knows the correct
    realm and client_id without hardcoding them.
    """
    if subdomain:
        result = await db.execute(
            select(TenantRealm)
            .join(Tenant, Tenant.id == TenantRealm.tenant_id)
            .where(Tenant.slug == subdomain)
            .where(TenantRealm.is_active == True)  # noqa: E712
        )
        realm_config = result.scalars().first()
        if realm_config:
            return RealmConfigResponse(
                realm_name=realm_config.realm_name,
                client_id=settings.KEYCLOAK_CLIENT_ID,
                issuer_url=realm_config.issuer_url,
            )

    # Default — shared unified-crm realm
    return RealmConfigResponse(
        realm_name=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_CLIENT_ID,
        issuer_url=f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
    )


# ---------------------------------------------------------------------------
# GET /auth/me
# Called by AuthContext after login to get tenant_id + role
# ---------------------------------------------------------------------------

@router.get("/me", response_model=MeResponse)
async def get_me(
    user: CurrentUser = Depends(get_current_user),
):
    """
    Returns the authenticated user's details.

    Frontend AuthContext calls this right after Keycloak login to:
      1. Get tenant_id (may not be in JWT claim during setup)
      2. Confirm role
      3. Confirm user is in dashboard_users table
    """
    return MeResponse(
        sub=user.sub,
        email=user.email,
        name=user.name,
        roles=user.roles,
        tenant_id=user.tenant_id,
        is_admin=user.is_admin,
        is_agent=user.is_agent,
        is_superadmin=user.is_superadmin,
    )