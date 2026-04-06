"""
app/core/keycloak_admin.py

Keycloak Admin REST API client.

Used by:
  - super_admin routes  (create tenant, invite org admin)
  - invitation routes   (invite agent, accept invite)

All functions accept an optional `realm` param defaulting to KEYCLOAK_REALM.
When a tenant gets its own realm later, just pass realm="acme-corp" — identical logic.
"""
from __future__ import annotations

import logging

import httpx

from app.core.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def _get_admin_token(realm: str | None = None) -> str:
    """Get a short-lived service-account admin token from Keycloak."""
    realm = realm or settings.KEYCLOAK_REALM
    url = f"{settings.KEYCLOAK_URL}/realms/{realm}/protocol/openid-connect/token"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.KEYCLOAK_ADMIN_CLIENT_ID,
                "client_secret": settings.KEYCLOAK_ADMIN_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def create_keycloak_user(
    email: str,
    first_name: str,
    last_name: str,
    tenant_id: str,
    role: str,
    realm: str | None = None,
) -> str:
    """
    Create a Keycloak user (enabled=True, emailVerified=False).
    Sets tenant_id as a user attribute so the JWT mapper can include it.
    Returns keycloak_sub (UUID string).
    Raises ValueError if user already exists (409 from Keycloak).
    """
    realm = realm or settings.KEYCLOAK_REALM
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"
    token = await _get_admin_token(realm)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{admin_base}/users",
            headers=headers,
            json={
                "email": email,
                "username": email,
                "firstName": first_name,
                "lastName": last_name,
                "enabled": True,
                "emailVerified": False,
                "attributes": {"tenant_id": [tenant_id]},
            },
        )

        if resp.status_code == 409:
            raise ValueError(f"User {email} already exists in realm {realm}")
        resp.raise_for_status()

        # Extract user UUID from Location header
        location = resp.headers.get("Location", "")
        keycloak_sub = location.split("/")[-1]

        # Assign realm role
        await _assign_realm_role(keycloak_sub, role, realm, token)
        logger.info("Created Keycloak user %s in realm %s with role %s", email, realm, role)
        return keycloak_sub


async def _assign_realm_role(
    user_id: str,
    role_name: str,
    realm: str,
    token: str,
) -> None:
    """Assign a realm-level role to a Keycloak user."""
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        role_resp = await client.get(f"{admin_base}/roles/{role_name}", headers=headers)
        role_resp.raise_for_status()
        role_rep = role_resp.json()

        assign_resp = await client.post(
            f"{admin_base}/users/{user_id}/role-mappings/realm",
            headers=headers,
            json=[role_rep],
        )
        assign_resp.raise_for_status()


async def disable_user(keycloak_sub: str, realm: str | None = None) -> None:
    """Soft-disable a Keycloak user (they cannot log in)."""
    realm = realm or settings.KEYCLOAK_REALM
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"
    token = await _get_admin_token(realm)

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{admin_base}/users/{keycloak_sub}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"enabled": False},
        )
        resp.raise_for_status()
        logger.info("Disabled Keycloak user %s in realm %s", keycloak_sub, realm)