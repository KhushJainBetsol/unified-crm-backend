"""
app/core/keycloak_admin.py

Keycloak Admin REST API client.

Used by:
  - super_admin routes  (create tenant, invite org admin)
  - invitation routes   (invite agent, accept invite)

All public functions accept an optional `realm` param defaulting to KEYCLOAK_REALM.
When a tenant gets its own realm later, just pass realm="acme-corp" — identical logic.

Public surface
--------------
create_keycloak_user    create + role-assign a new user
update_keycloak_user    partial update (name / email / enabled flag)
delete_keycloak_user    hard-delete from Keycloak (idempotent on 404)
disable_user            convenience wrapper — sets enabled=False
"""

from __future__ import annotations

import logging

import httpx

from app.core.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_admin_token(realm: str | None = None) -> str:
    """
    Obtain a short-lived service-account token from Keycloak.
    Uses client_credentials — no human interaction required.
    """
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
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _assign_realm_role(
    user_id: str,
    role_name: str,
    realm: str,
    token: str,
) -> None:
    """Assign a realm-level role to a Keycloak user by role name."""
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        # Fetch role representation first — Keycloak requires the full object
        role_resp = await client.get(
            f"{admin_base}/roles/{role_name}",
            headers=headers,
            timeout=10,
        )
        role_resp.raise_for_status()

        assign_resp = await client.post(
            f"{admin_base}/users/{user_id}/role-mappings/realm",
            headers=headers,
            json=[role_resp.json()],
            timeout=10,
        )
        assign_resp.raise_for_status()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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

    Sets ``tenant_id`` as a user attribute so the JWT mapper can embed it
    in the token as a custom claim.

    Returns
    -------
    str
        The Keycloak subject UUID (``keycloak_sub``) — store this on
        ``dashboard_users.keycloak_sub`` so you can target the user in
        future Admin API calls without an extra lookup.

    Raises
    ------
    ValueError
        If a user with this email already exists in the realm (HTTP 409).
    httpx.HTTPStatusError
        For any other Keycloak error.
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
                "username": email,          # username == email in this setup
                "firstName": first_name,
                "lastName": last_name,
                "enabled": True,
                "emailVerified": False,
                "attributes": {"tenant_id": [tenant_id]},
            },
            timeout=10,
        )

        if resp.status_code == 409:
            raise ValueError(f"User '{email}' already exists in realm '{realm}'")
        resp.raise_for_status()

        # Keycloak returns the new user URL in the Location header:
        # .../admin/realms/{realm}/users/{uuid}
        location = resp.headers.get("Location", "")
        keycloak_sub = location.split("/")[-1]
        if not keycloak_sub:
            raise RuntimeError(
                "Keycloak did not return a Location header after user creation. "
                "Cannot determine keycloak_sub."
            )

    await _assign_realm_role(keycloak_sub, role, realm, token)
    logger.info("Created Keycloak user '%s' in realm '%s' with role '%s'.", email, realm, role)
    return keycloak_sub


async def update_keycloak_user(
    keycloak_sub: str,
    realm: str | None = None,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    enabled: bool | None = None,
) -> None:
    """
    Partially update a Keycloak user.

    Only fields that are explicitly supplied (not None) are sent, so callers
    can update a single attribute without knowing the current values of others.

    Parameters
    ----------
    keycloak_sub:
        The user's Keycloak UUID (``dashboard_users.keycloak_sub``).
    first_name / last_name / email:
        Profile fields.  Changing ``email`` also updates ``username``
        because we use email-as-username throughout.
    enabled:
        False  → user cannot log in (soft disable).
        True   → re-enable a previously disabled account.
    """
    realm = realm or settings.KEYCLOAK_REALM

    payload: dict = {}
    if first_name is not None:
        payload["firstName"] = first_name
    if last_name is not None:
        payload["lastName"] = last_name
    if email is not None:
        payload["email"] = email
        payload["username"] = email     # keep username in sync
    if enabled is not None:
        payload["enabled"] = enabled

    if not payload:
        logger.debug("update_keycloak_user called with no fields to change — skipping.")
        return

    token = await _get_admin_token(realm)
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{admin_base}/users/{keycloak_sub}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 404:
            logger.warning(
                "Keycloak user %s not found in realm %s during update — skipping.",
                keycloak_sub, realm,
            )
            return
        resp.raise_for_status()

    logger.info(
        "Updated Keycloak user %s in realm %s: fields=%s",
        keycloak_sub, realm, list(payload.keys()),
    )


async def delete_keycloak_user(
    keycloak_sub: str,
    realm: str | None = None,
) -> None:
    """
    Permanently delete a user from Keycloak.

    Idempotent — a 404 response is silently ignored so callers can retry
    without extra existence checks.

    Important: this is NOT transactional with your Postgres writes.
    Always commit the DB changes first, then call this function.
    If this call fails, log a warning and surface it in the API response —
    the user's existing tokens will stop working once they expire (default
    5 min for Keycloak access tokens), so the exposure window is small.
    """
    realm = realm or settings.KEYCLOAK_REALM
    token = await _get_admin_token(realm)
    admin_base = f"{settings.KEYCLOAK_URL}/admin/realms/{realm}"

    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{admin_base}/users/{keycloak_sub}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    if resp.status_code == 404:
        logger.warning(
            "Keycloak user %s not found in realm %s — already deleted, skipping.",
            keycloak_sub, realm,
        )
        return

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Keycloak hard-delete failed for sub=%s in realm=%s: %s",
            keycloak_sub, realm, exc,
        )
        raise

    logger.info("Hard-deleted Keycloak user %s from realm %s.", keycloak_sub, realm)


async def disable_user(keycloak_sub: str, realm: str | None = None) -> None:
    """
    Convenience wrapper — soft-disable a user (they cannot log in).

    Delegates to ``update_keycloak_user`` with ``enabled=False``.
    Kept for backwards compatibility with existing call sites.
    """
    await update_keycloak_user(keycloak_sub, realm, enabled=False)
    logger.info("Disabled Keycloak user %s in realm %s.", keycloak_sub, realm or settings.KEYCLOAK_REALM)