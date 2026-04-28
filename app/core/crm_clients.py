# """
# app/core/crm_clients.py

# Thin async HTTP clients for fetching an organisation / account ID
# from each supported external CRM.

# Supported systems (matched by source_systems.system_name):
#   - "zammad"   → GET /api/v1/organizations   (searches by name)
#   - "espocrm"  → GET /api/v1/Account         (searches by name)

# Returns the CRM's own string ID for the first matching record,
# or None if nothing is found or the call fails.

# All failures are soft — callers should log and continue rather
# than aborting tenant creation.
# """

# from __future__ import annotations

# import logging
# from typing import Any

# import httpx

# from app.core.settings import get_settings

# settings = get_settings()
# logger = logging.getLogger(__name__)

# # How long to wait for each CRM before giving up (seconds)
# _HTTP_TIMEOUT = 10.0


# # ---------------------------------------------------------------------------
# # Internal helpers
# # ---------------------------------------------------------------------------

# def _zammad_headers() -> dict[str, str]:
#     return {
#         "Authorization": f"Token token={settings.ZAMMAD_API_TOKEN}",
#         "Content-Type": "application/json",
#     }


# def _espo_headers() -> dict[str, str]:
#     return {
#         "X-Api-Key": settings.ESPO_API_KEY,
#         "Content-Type": "application/json",
#     }


# async def _fetch_zammad_org_id(tenant_name: str) -> str | None:
#     """
#     Search Zammad for an organisation whose name matches *tenant_name*.

#     Endpoint : GET /api/v1/organizations
#     Query    : ?query=<tenant_name>&limit=1
#     Response : list of organisation objects, each with an "id" integer field.

#     Returns the id (as a string) of the first hit, or None.
#     """
#     url = f"{settings.ZAMMAD_BASE_URL.rstrip('/')}/api/v1/organizations/search"
#     params = {"query": f"name:{tenant_name}"}

#     try:
#         async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
#             resp = await client.get(url, headers=_zammad_headers(), params=params)
#             resp.raise_for_status()
#             data = resp.json()

#             # Zammad returns a plain list of org objects
#             if isinstance(data, list) and data:
#                 org_id = data[0].get("id")
#                 if org_id is not None:
#                     logger.info(
#                         "Zammad org found for tenant '%s': id=%s", tenant_name, org_id
#                     )
#                     return str(org_id)

#             logger.warning(
#                 "Zammad: no organisation found for tenant '%s'", tenant_name
#             )
#             return None

#     except httpx.HTTPStatusError as exc:
#         logger.warning(
#             "Zammad API returned %s for tenant '%s': %s",
#             exc.response.status_code, tenant_name, exc.response.text,
#         )
#         return None
#     except Exception as exc:
#         logger.warning(
#             "Zammad org lookup failed for tenant '%s': %s", tenant_name, exc
#         )
#         return None


# async def _fetch_espo_account_id(tenant_name: str) -> str | None:
#     """
#     Search EspoCRM for an Account whose name matches *tenant_name*.
#     """

#     url = f"{settings.ESPO_BASE_URL.rstrip('/')}/api/v1/Account"

#     params = {
#         "where[0][type]": "equals",
#         "where[0][attribute]": "name",
#         "where[0][value]": tenant_name,
#         "maxSize": 1,
#     }

#     try:
#         async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
#             resp = await client.get(
#                 url,
#                 headers=_espo_headers(),
#                 params=params,
#             )
#             resp.raise_for_status()

#             data = resp.json()
#             records = data.get("list", [])

#             if records:
#                 account_id = records[0].get("id")
#                 if account_id:
#                     logger.info(
#                         "EspoCRM account found for tenant '%s': id=%s",
#                         tenant_name,
#                         account_id,
#                     )
#                     return str(account_id)

#             logger.warning("EspoCRM: no Account found for tenant '%s'", tenant_name)
#             return None

#     except httpx.HTTPStatusError as exc:
#         logger.warning(
#             "EspoCRM API returned %s for tenant '%s': %s",
#             exc.response.status_code,
#             tenant_name,
#             exc.response.text,
#         )
#         return None
#     except Exception as exc:
#         logger.warning(
#             "EspoCRM account lookup failed for tenant '%s': %s",
#             tenant_name,
#             exc,
#         )
#         return None


# # ---------------------------------------------------------------------------
# # Public interface
# # ---------------------------------------------------------------------------

# async def fetch_crm_org_id(system_name: str, tenant_name: str) -> str | None:
#     """
#     Dispatch to the correct CRM client based on *system_name* and return
#     the external organisation / account ID for *tenant_name*.

#     Args:
#         system_name:  Value from source_systems.system_name  ("zammad" | "espocrm").
#         tenant_name:  The tenant's display name used as the search term.

#     Returns:
#         The CRM's own ID string, or None if not found / on any error.
#         Never raises — all failures are swallowed and logged so that
#         tenant creation is not blocked by CRM unavailability.
#     """
#     normalised = system_name.strip().lower()

#     if normalised == "zammad":
#         return await _fetch_zammad_org_id(tenant_name)

#     if normalised == "espocrm":
#         return await _fetch_espo_account_id(tenant_name)

#     # Unknown system — nothing to fetch
#     logger.debug(
#         "fetch_crm_org_id: no CRM client configured for system '%s'", system_name
#     )
#     return None

"""
app/core/crm_clients.py

Thin async HTTP helper for resolving a CRM organisation / account ID
during tenant onboarding — ADAPTER PATTERN (migrated).

WHAT CHANGED
------------
The original file used raw httpx calls with hard-coded credentials read
from global Settings (ZAMMAD_API_TOKEN, ESPO_API_KEY).  This bypassed the
adapter layer entirely and would break the moment a tenant has per-tenant
credentials stored in Infisical.

NEW APPROACH
------------
This module now uses the CrmAdapterFactory (from app.state) to build a
proper authenticated adapter for the integration_id supplied at call time,
then calls adapter.fetch_organizations() to find the matching org by name.

If no integration_id is available yet (e.g., during initial tenant setup
before credentials are saved), a lightweight fallback using the global
settings credentials is still provided as _fetch_org_id_fallback().
That fallback is only for the bootstrap / onboarding path and should be
removed once all tenants have integration records.

USAGE (called from credential_service.py)
------------------------------------------
    from app.core.crm_clients import fetch_crm_org_id

    crm_org_id = await fetch_crm_org_id(
        system_name=source_system_name,
        tenant_name=tenant_display_name,
        factory=app.state.adapter_factory,       # preferred
        integration_id=tss.integration_id,       # preferred
    )
"""

from __future__ import annotations

import logging
from typing import Optional

from app.adapters.base.client import CrmClientError
from app.core.settings import get_settings
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory

logger = logging.getLogger(__name__)
settings = get_settings()

_HTTP_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def fetch_crm_org_id(
    system_name: str,
    tenant_name: str,
    factory: Optional[CrmAdapterFactory] = None,
    integration_id: Optional[str] = None,
) -> str | None:
    """
    Resolve the CRM-native organisation/account ID for *tenant_name*.

    Tries the adapter path first (preferred), falls back to the legacy
    hard-coded-credentials path if factory/integration_id are unavailable.

    Args:
        system_name:     Value from source_systems.system_name ("zammad" | "espocrm").
        tenant_name:     The tenant's display name used as the search term.
        factory:         CrmAdapterFactory from app.state (preferred).
        integration_id:  UUID string of the CrmIntegration row (preferred).

    Returns:
        The CRM's own ID string, or None if not found / on any error.
        Never raises — all failures are swallowed and logged.
    """
    if factory is not None and integration_id is not None:
        return await _fetch_via_adapter(system_name, tenant_name, factory, integration_id)

    # Fallback: legacy path using global settings credentials
    logger.warning(
        "fetch_crm_org_id: factory/integration_id not provided for system='%s' — "
        "using legacy credential fallback for tenant '%s'",
        system_name, tenant_name,
    )
    return await _fetch_org_id_fallback(system_name, tenant_name)


# ---------------------------------------------------------------------------
# Adapter-pattern path (preferred)
# ---------------------------------------------------------------------------

async def _fetch_via_adapter(
    system_name: str,
    tenant_name: str,
    factory: CrmAdapterFactory,
    integration_id: str,
) -> str | None:
    """
    Use the adapter to search for an organisation matching tenant_name.
    Fetches all organisations and returns the first whose name matches.
    """
    try:
        adapter = await factory.create(integration_id)
    except AdapterFactoryError as exc:
        logger.warning(
            "fetch_crm_org_id: could not build adapter for integration_id='%s': %s",
            integration_id, exc,
        )
        return None

    try:
        async with adapter:
            result = await adapter.fetch_organizations()

        for org in result.items:
            org_name = getattr(org, "name", None) or ""
            if org_name.lower() == tenant_name.lower():
                logger.info(
                    "%s org found for tenant '%s': id=%s",
                    system_name, tenant_name, org.id,
                )
                return org.id

        logger.warning(
            "%s: no organisation named '%s' found via adapter (integration_id=%s)",
            system_name, tenant_name, integration_id,
        )
        return None

    except CrmClientError as exc:
        logger.warning(
            "CRM client error looking up org for tenant '%s' (system=%s): %s",
            tenant_name, system_name, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Unexpected error looking up org for tenant '%s' (system=%s): %s",
            tenant_name, system_name, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Legacy fallback — uses global settings credentials
# Keep only for the bootstrap / onboarding path.
# TODO: Remove once all tenants have integration records in the DB.
# ---------------------------------------------------------------------------

async def _fetch_org_id_fallback(system_name: str, tenant_name: str) -> str | None:
    """
    Legacy fallback: direct httpx calls using credentials from Settings.
    Only used when no integration_id is available yet.
    """
    import httpx

    normalised = system_name.strip().lower()

    if normalised == "zammad":
        return await _fetch_zammad_org_id_legacy(tenant_name)
    if normalised == "espocrm":
        return await _fetch_espo_account_id_legacy(tenant_name)

    logger.debug(
        "fetch_crm_org_id fallback: no client configured for system '%s'", system_name
    )
    return None


async def _fetch_zammad_org_id_legacy(tenant_name: str) -> str | None:
    import httpx

    url = f"{settings.ZAMMAD_BASE_URL.rstrip('/')}/api/v1/organizations/search"
    params = {"query": f"name:{tenant_name}"}
    headers = {
        "Authorization": f"Token token={settings.ZAMMAD_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                org_id = data[0].get("id")
                if org_id is not None:
                    return str(org_id)
        logger.warning("Zammad fallback: no organisation found for tenant '%s'", tenant_name)
        return None
    except Exception as exc:
        logger.warning("Zammad fallback org lookup failed for tenant '%s': %s", tenant_name, exc)
        return None


async def _fetch_espo_account_id_legacy(tenant_name: str) -> str | None:
    import httpx

    url = f"{settings.ESPO_BASE_URL.rstrip('/')}/api/v1/Account"
    params = {
        "where[0][type]": "equals",
        "where[0][attribute]": "name",
        "where[0][value]": tenant_name,
        "maxSize": 1,
    }
    headers = {
        "X-Api-Key": settings.ESPO_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            records = data.get("list", [])
            if records:
                account_id = records[0].get("id")
                if account_id:
                    return str(account_id)
        logger.warning("EspoCRM fallback: no Account found for tenant '%s'", tenant_name)
        return None
    except Exception as exc:
        logger.warning("EspoCRM fallback account lookup failed for tenant '%s': %s", tenant_name, exc)
        return None