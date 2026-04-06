"""
app/core/auth.py

Keycloak JWT validation — drop-in replacement for the old no-auth setup.

Flow per request:
  1. Extract Bearer token from Authorization header
  2. Read issuer from UNVERIFIED claims (safe — only used for lookup)
  3. Verify issuer exists in tenant_realms table (whitelist check)
  4. Fetch Keycloak JWKS for that issuer (cached 1 hour)
  5. Validate signature, expiry, issuer
  6. Build CurrentUser — extracts tenant_id, role from claims
     (falls back to dashboard_users DB lookup if JWT claim is missing)

NO changes to existing routes yet — auth is additive.
"""

from __future__ import annotations

import logging
import threading
from typing import Annotated

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=True)

# JWKS cache — keyed by issuer_url, TTL 1 hour
_JWKS_CACHE: TTLCache = TTLCache(maxsize=50, ttl=3600)
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Allowed issuers — read from tenant_realms table every request
# ---------------------------------------------------------------------------

async def _get_allowed_issuers(db: AsyncSession) -> set[str]:
    result = await db.execute(
        text("SELECT issuer_url FROM tenant_realms WHERE is_active = true")
    )
    rows = result.fetchall()
    issuers = {row[0] for row in rows}
    logger.debug("Allowed issuers: %s", issuers)
    return issuers


# ---------------------------------------------------------------------------
# JWKS fetcher — cached per issuer
# ---------------------------------------------------------------------------

def _get_jwks_for_issuer(issuer_url: str, force_refresh: bool = False) -> dict:
    with _CACHE_LOCK:
        if not force_refresh and issuer_url in _JWKS_CACHE:
            return _JWKS_CACHE[issuer_url]

    jwks_url = f"{issuer_url}/protocol/openid-connect/certs"
    try:
        resp = httpx.get(jwks_url, timeout=10)
        resp.raise_for_status()
        jwks = resp.json()
        with _CACHE_LOCK:
            _JWKS_CACHE[issuer_url] = jwks
        return jwks
    except httpx.HTTPError as e:
        logger.error("JWKS fetch failed for %s: %s", issuer_url, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )


# ---------------------------------------------------------------------------
# Token decoder — with one JWKS retry on failure (handles key rotation)
# ---------------------------------------------------------------------------

def _decode_token(token: str, allowed_issuers: set[str]) -> dict:
    try:
        unverified = jwt.get_unverified_claims(token)
        issuer = unverified.get("iss")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token")

    if not issuer or issuer not in allowed_issuers:
        logger.warning("Token from unknown issuer: %s | allowed: %s", issuer, allowed_issuers)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token issuer")

    for attempt in range(2):
        try:
            jwks = _get_jwks_for_issuer(issuer, force_refresh=(attempt == 1))
            return jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                issuer=issuer,
                options={"verify_aud": False, "verify_at_hash": False},
            )
        except ExpiredSignatureError:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTError:
            if attempt == 0:
                continue
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


# ---------------------------------------------------------------------------
# CurrentUser — populated from validated JWT claims
# ---------------------------------------------------------------------------

class CurrentUser:
    def __init__(self, payload: dict) -> None:
        self.sub: str = payload["sub"]
        self.email: str = payload.get("email", "")
        self.name: str = payload.get("name", "")
        self.tenant_id: str | None = payload.get("tenant_id")

        # Read roles from all Keycloak formats
        roles: list[str] = []

        # Format 1 — custom mapper: "roles": ["admin"]
        if payload.get("roles"):
            roles = [r for r in payload["roles"] if r in ("admin", "agent", "superadmin")]

        # Format 2 — Keycloak default: "realm_access": {"roles": ["admin"]}
        if not roles:
            realm_roles = payload.get("realm_access", {}).get("roles", [])
            roles = [r for r in realm_roles if r in ("admin", "agent", "superadmin")]

        # Format 3 — client roles fallback
        if not roles:
            for client_data in payload.get("resource_access", {}).values():
                for r in client_data.get("roles", []):
                    if r in ("admin", "agent", "superadmin"):
                        roles.append(r)

        self.roles: list[str] = roles
        logger.debug(
            "CurrentUser: sub=%s roles=%s tenant_id=%s",
            self.sub, self.roles, self.tenant_id,
        )

    @property
    def is_superadmin(self) -> bool:
        return "superadmin" in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_agent(self) -> bool:
        return "agent" in self.roles

    def require_tenant(self) -> str:
        if not self.tenant_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Account not fully configured — tenant_id missing. Contact your administrator.",
            )
        return self.tenant_id


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """
    Validate JWT and return CurrentUser.

    Falls back to dashboard_users DB lookup if tenant_id is missing from token
    (covers Keycloak mapper not yet configured for a realm).
    """
    allowed_issuers = await _get_allowed_issuers(db)
    payload = _decode_token(credentials.credentials, allowed_issuers)
    user = CurrentUser(payload)

    # DB fallback for tenant_id
    if not user.tenant_id:
        try:
            result = await db.execute(
                text("""
                    SELECT tenant_id::text
                    FROM dashboard_users
                    WHERE keycloak_sub = :sub
                    AND is_active = true
                    LIMIT 1
                """),
                {"sub": user.sub},
            )
            row = result.fetchone()
            if row and row[0]:
                user.tenant_id = str(row[0])
                logger.info("tenant_id from DB fallback: sub=%s → %s", user.sub, user.tenant_id)
            else:
                logger.warning("No dashboard_users row for sub=%s", user.sub)
        except Exception as e:
            logger.error("DB fallback for tenant_id failed: %s", e)

    return user


async def require_admin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if not user.is_admin and not user.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


async def require_agent(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if not user.is_agent and not user.is_admin and not user.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent access required")
    return user


async def require_superadmin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if not user.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Superadmin access required")
    return user