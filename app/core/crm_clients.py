"""
app/core/crm_clients.py

Thin async HTTP clients for fetching an organisation / account ID
from each supported external CRM.

Supported systems (matched by source_systems.system_name):
  - "zammad"   → GET /api/v1/organizations   (searches by name)
  - "espocrm"  → GET /api/v1/Account         (searches by name)

Returns the CRM's own string ID for the first matching record,
or None if nothing is found or the call fails.

All failures are soft — callers should log and continue rather
than aborting tenant creation.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# How long to wait for each CRM before giving up (seconds)
_HTTP_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _zammad_headers() -> dict[str, str]:
    return {
        "Authorization": f"Token token={settings.ZAMMAD_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _espo_headers() -> dict[str, str]:
    return {
        "X-Api-Key": settings.ESPO_API_KEY,
        "Content-Type": "application/json",
    }


async def _fetch_zammad_org_id(tenant_name: str) -> str | None:
    """
    Search Zammad for an organisation whose name matches *tenant_name*.

    Endpoint : GET /api/v1/organizations
    Query    : ?query=<tenant_name>&limit=1
    Response : list of organisation objects, each with an "id" integer field.

    Returns the id (as a string) of the first hit, or None.
    """
    url = f"{settings.ZAMMAD_BASE_URL.rstrip('/')}/api/v1/organizations/search"
    params = {"query": f"name:{tenant_name}"}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_zammad_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()

            # Zammad returns a plain list of org objects
            if isinstance(data, list) and data:
                org_id = data[0].get("id")
                if org_id is not None:
                    logger.info(
                        "Zammad org found for tenant '%s': id=%s", tenant_name, org_id
                    )
                    return str(org_id)

            logger.warning(
                "Zammad: no organisation found for tenant '%s'", tenant_name
            )
            return None

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Zammad API returned %s for tenant '%s': %s",
            exc.response.status_code, tenant_name, exc.response.text,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Zammad org lookup failed for tenant '%s': %s", tenant_name, exc
        )
        return None


async def _fetch_espo_account_id(tenant_name: str) -> str | None:
    """
    Search EspoCRM for an Account whose name matches *tenant_name*.
    """

    url = f"{settings.ESPO_BASE_URL.rstrip('/')}/api/v1/Account"

    params = {
        "where[0][type]": "equals",
        "where[0][attribute]": "name",
        "where[0][value]": tenant_name,
        "maxSize": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers=_espo_headers(),
                params=params,
            )
            resp.raise_for_status()

            data = resp.json()
            records = data.get("list", [])

            if records:
                account_id = records[0].get("id")
                if account_id:
                    logger.info(
                        "EspoCRM account found for tenant '%s': id=%s",
                        tenant_name,
                        account_id,
                    )
                    return str(account_id)

            logger.warning("EspoCRM: no Account found for tenant '%s'", tenant_name)
            return None

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "EspoCRM API returned %s for tenant '%s': %s",
            exc.response.status_code,
            tenant_name,
            exc.response.text,
        )
        return None
    except Exception as exc:
        logger.warning(
            "EspoCRM account lookup failed for tenant '%s': %s",
            tenant_name,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def fetch_crm_org_id(system_name: str, tenant_name: str) -> str | None:
    """
    Dispatch to the correct CRM client based on *system_name* and return
    the external organisation / account ID for *tenant_name*.

    Args:
        system_name:  Value from source_systems.system_name  ("zammad" | "espocrm").
        tenant_name:  The tenant's display name used as the search term.

    Returns:
        The CRM's own ID string, or None if not found / on any error.
        Never raises — all failures are swallowed and logged so that
        tenant creation is not blocked by CRM unavailability.
    """
    normalised = system_name.strip().lower()

    if normalised == "zammad":
        return await _fetch_zammad_org_id(tenant_name)

    if normalised == "espocrm":
        return await _fetch_espo_account_id(tenant_name)

    # Unknown system — nothing to fetch
    logger.debug(
        "fetch_crm_org_id: no CRM client configured for system '%s'", system_name
    )
    return None