"""
app/integrations/espo/client.py

Low-level async HTTP client for the EspoCRM REST API.

Responsibilities:
  - Authentication via API Key header
  - Raw HTTP requests to EspoCRM endpoints
  - HTTP error handling
  - Pagination using EspoCRM's offset/maxSize pattern

EspoCRM API quirks vs Zammad:
  - Auth header is "X-Api-Key" not "Authorization: Bearer"
  - Pagination uses offset + maxSize (not page number)
  - List responses are wrapped: { "list": [...], "total": N }
  - Entity for tickets is "Case" not "Ticket"
  - Timestamps use camelCase: "createdAt", "modifiedAt"

EspoCRM API docs: https://docs.espocrm.com/development/api/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

ESPO_PAGE_SIZE = 200

AGENT_ROLE_KEYWORD    = "Agent"
CUSTOMER_ROLE_KEYWORD = "Customer"


class EspoClientError(Exception):
    pass


class EspoAuthError(EspoClientError):
    pass


class EspoClient:
    """
    Async HTTP client for EspoCRM REST API.

    Usage:
        async with EspoClient() as client:
            tickets = await client.get_all_tickets()
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ESPO_BASE_URL).rstrip("/")
        self._api_key  = api_key or settings.ESPO_API_KEY
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "EspoClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-Api-Key":    self._api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "EspoClient must be used as an async context manager: "
                "`async with EspoClient() as client:`"
            )
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> Any:
        client = self._ensure_client()
        logger.debug("EspoCRM GET %s params=%s", path, params)
        response = await client.get(path, params=params)
        if response.status_code in (401, 403):
            raise EspoAuthError(
                f"EspoCRM authentication failed ({response.status_code}). "
                "Check ESPO_API_KEY in your .env"
            )
        if not response.is_success:
            raise EspoClientError(
                f"EspoCRM API error {response.status_code} for GET {path}: "
                f"{response.text[:300]}"
            )
        return response.json()

    async def _put(self, path: str, data: dict) -> Any:
        client = self._ensure_client()
        logger.debug("EspoCRM PUT %s payload=%s", path, data)
        response = await client.put(path, json=data)
        if response.status_code in (401, 403):
            raise EspoAuthError(
                f"EspoCRM authentication failed ({response.status_code}). "
                "Check ESPO_API_KEY in your .env"
            )
        if not response.is_success:
            raise EspoClientError(
                f"EspoCRM API error {response.status_code} for PUT {path}: "
                f"{response.text[:300]}"
            )
        return response.json()

    async def _post(self, path: str, data: dict) -> Any:
        client = self._ensure_client()
        logger.debug("EspoCRM POST %s", path)
        response = await client.post(path, json=data)
        if response.status_code in (401, 403):
            raise EspoAuthError(
                f"EspoCRM authentication failed ({response.status_code}). "
                "Check ESPO_API_KEY in your .env"
            )
        if not response.is_success:
            raise EspoClientError(
                f"EspoCRM API error {response.status_code} for POST {path}: "
                f"{response.text[:300]}"
            )
        return response.json()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_case_field_options(self) -> dict[str, list[str]]:
        response = await self._get("/api/v1/metadata", params={"scopes[]": "Case"})
        fields: dict = response.get("entityDefs", {}).get("Case", {}).get("fields", {})
        return {
            field_name: field_def.get("options", [])
            for field_name, field_def in fields.items()
            if field_def.get("options")
        }

    # ------------------------------------------------------------------
    # User helpers
    # ------------------------------------------------------------------
    async def _get_all_users_with_detail(self) -> list[dict]:
        all_ids: list[str] = []
        offset = 0
        while True:
            response = await self._get(
                "/api/v1/User",
                params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
            )
            batch: list[dict] = response.get("list", [])
            total: int        = response.get("total", 0)
            if not batch:
                break
            all_ids.extend(user["id"] for user in batch)
            offset += ESPO_PAGE_SIZE
            if len(all_ids) >= total:
                break

        logger.info("EspoCRM: found %d user IDs, fetching full details...", len(all_ids))
        detailed_users: list[dict] = []
        for i, user_id in enumerate(all_ids, start=1):
            logger.debug("Fetching user detail %d/%d  id=%s", i, len(all_ids), user_id)
            try:
                detail = await self._get(f"/api/v1/User/{user_id}")
                detailed_users.append(detail)
            except EspoClientError as exc:
                logger.warning("Skipping user %s — failed to fetch detail: %s", user_id, exc)

        logger.info(
            "EspoCRM: fetched full details for %d / %d users",
            len(detailed_users), len(all_ids),
        )
        return detailed_users

    # ------------------------------------------------------------------
    # Ticket (Case) endpoints
    # ------------------------------------------------------------------
    async def get_ticket_by_id(self, ticket_id: str) -> dict:
        return await self._get(f"/api/v1/Case/{ticket_id}")

    async def get_tickets(
        self,
        offset: int = 0,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> tuple[list[dict], int]:
        response = await self._get(
            "/api/v1/Case",
            params={
                "offset":  offset,
                "maxSize": min(max_size, ESPO_PAGE_SIZE),
                "orderBy": "createdAt",
                "order":   "asc",
            },
        )
        return response.get("list", []), response.get("total", 0)

    async def get_all_tickets(self, max_size: int = ESPO_PAGE_SIZE) -> list[dict]:
        all_tickets: list[dict] = []
        offset = 0
        batch, total = await self.get_tickets(offset=offset, max_size=max_size)
        all_tickets.extend(batch)
        logger.info("EspoCRM: fetched %d tickets (total: %d)", len(all_tickets), total)
        while len(all_tickets) < total:
            offset += max_size
            logger.info("Fetching EspoCRM tickets offset=%d", offset)
            batch, _ = await self.get_tickets(offset=offset, max_size=max_size)
            if not batch:
                break
            all_tickets.extend(batch)
        logger.info("EspoCRM: fetched %d tickets total", len(all_tickets))
        return all_tickets

    async def get_tickets_by_account(
        self,
        crm_org_id: str,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch ALL Cases belonging to a specific EspoCRM Account (organization).

        Uses the where filter:
          GET /api/v1/Case
            ?where[0][type]=equals
            &where[0][attribute]=accountId
            &where[0][value]=<crm_org_id>

        Args:
            crm_org_id: EspoCRM Account UUID string (crm_org_id from tenant_source_systems).
            max_size:   Records per request (max 200).

        Returns:
            Flat list of all raw case dicts for that account.
        """
        all_tickets: list[dict] = []
        offset = 0

        while True:
            logger.info(
                "Fetching EspoCRM tickets for account_id=%s offset=%d",
                crm_org_id, offset,
            )
            response = await self._get(
                "/api/v1/Case",
                params={
                    "offset":               offset,
                    "maxSize":              min(max_size, ESPO_PAGE_SIZE),
                    "orderBy":              "createdAt",
                    "order":               "asc",
                    "where[0][type]":       "equals",
                    "where[0][attribute]":  "accountId",
                    "where[0][value]":      crm_org_id,
                },
            )
            batch: list[dict] = response.get("list", [])
            total: int        = response.get("total", 0)

            if not batch:
                break

            all_tickets.extend(batch)
            logger.info(
                "EspoCRM account=%s: fetched %d tickets (total so far: %d / %d)",
                crm_org_id, len(batch), len(all_tickets), total,
            )
            offset += max_size

            if len(all_tickets) >= total:
                break

        logger.info(
            "EspoCRM: fetched %d tickets total for account_id=%s",
            len(all_tickets), crm_org_id,
        )
        return all_tickets

    async def get_tickets_updated_since(self, since: str) -> list[dict]:
        logger.info("Fetching EspoCRM cases updated since %s", since)
        all_tickets: list[dict] = []
        offset = 0
        while True:
            response = await self._get(
                "/api/v1/Case",
                params={
                    "offset":               offset,
                    "maxSize":              ESPO_PAGE_SIZE,
                    "orderBy":              "modifiedAt",
                    "order":                "asc",
                    "where[0][type]":       "after",
                    "where[0][attribute]":  "modifiedAt",
                    "where[0][value]":      since,
                },
            )
            batch = response.get("list", [])
            total = response.get("total", 0)
            if not batch:
                break
            all_tickets.extend(batch)
            offset += ESPO_PAGE_SIZE
            if len(all_tickets) >= total:
                break
        return all_tickets

    async def update_ticket(self, crm_ticket_id: str, data: dict) -> dict:
        return await self._put(f"/api/v1/Case/{crm_ticket_id}", data)

    # ------------------------------------------------------------------
    # Agent (User) endpoints
    # ------------------------------------------------------------------
    async def get_all_agents(self) -> list[dict]:
        all_users = await self._get_all_users_with_detail()
        agents = [
            user for user in all_users
            if any(
                AGENT_ROLE_KEYWORD in role_name
                for role_name in (user.get("rolesNames") or {}).values()
            )
        ]
        logger.info("EspoCRM: fetched %d agents total", len(agents))
        return agents

    async def get_agents_by_account(self, crm_org_id: str) -> list[dict]:
        """
        Fetch EspoCRM users (agents) associated with a specific Account.

        EspoCRM users don't have a direct accountId FK — the relationship
        is through Contacts. For agents we search by their assigned account
        via the Teams / Roles relationship. In practice most EspoCRM
        deployments scope agents at the system level, not per account.

        Strategy: fetch ALL users with detail and filter by role keyword.
        Then return all agents (agents are shared across the CRM instance,
        not per-account). Use get_contacts_by_account for customers.

        Args:
            crm_org_id: EspoCRM Account UUID string (informational; agents
                        are instance-scoped, not account-scoped in EspoCRM).
        """
        # Agents in EspoCRM are instance-level, not account-scoped.
        # We still return all agents so ticket FK resolution works correctly.
        logger.info(
            "EspoCRM: fetching all agents (instance-scoped) for account_id=%s", crm_org_id
        )
        return await self.get_all_agents()

    # ------------------------------------------------------------------
    # Customer (Contact) endpoints
    # ------------------------------------------------------------------
    async def get_all_customers(self) -> list[dict]:
        all_users = await self._get_all_users_with_detail()
        customers = [
            user for user in all_users
            if any(
                CUSTOMER_ROLE_KEYWORD in role_name
                for role_name in (user.get("rolesNames") or {}).values()
            )
        ]
        logger.info("EspoCRM: fetched %d customers total", len(customers))
        return customers

    async def get_contacts_by_account(
        self,
        crm_org_id: str,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch all EspoCRM Contacts linked to a specific Account.

        GET /api/v1/Contact
          ?where[0][type]=equals
          &where[0][attribute]=accountId
          &where[0][value]=<crm_org_id>

        Args:
            crm_org_id: EspoCRM Account UUID string.
            max_size:   Records per request (max 200).

        Returns:
            Flat list of raw Contact dicts for that account.
        """
        all_contacts: list[dict] = []
        offset = 0

        while True:
            logger.info(
                "Fetching EspoCRM contacts for account_id=%s offset=%d",
                crm_org_id, offset,
            )
            response = await self._get(
                "/api/v1/Contact",
                params={
                    "offset":               offset,
                    "maxSize":              min(max_size, ESPO_PAGE_SIZE),
                    "where[0][type]":       "equals",
                    "where[0][attribute]":  "accountId",
                    "where[0][value]":      crm_org_id,
                },
            )
            batch: list[dict] = response.get("list", [])
            total: int        = response.get("total", 0)

            if not batch:
                break

            all_contacts.extend(batch)
            offset += max_size

            if len(all_contacts) >= total:
                break

        logger.info(
            "EspoCRM: fetched %d contacts for account_id=%s",
            len(all_contacts), crm_org_id,
        )
        return all_contacts

    # ------------------------------------------------------------------
    # Company (Account) endpoints
    # ------------------------------------------------------------------
    async def get_all_companies(self) -> list[dict]:
        all_companies: list[dict] = []
        offset = 0
        while True:
            response = await self._get(
                "/api/v1/Account",
                params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
            )
            batch: list[dict] = response.get("list", [])
            total: int        = response.get("total", 0)
            all_companies.extend(batch)
            if len(all_companies) >= total or not batch:
                break
            offset += ESPO_PAGE_SIZE
        logger.info("EspoCRM: fetched %d companies total", len(all_companies))
        return all_companies

    async def get_account_by_id(self, crm_org_id: str) -> dict:
        """
        Fetch a single EspoCRM Account by its ID.

        GET /api/v1/Account/:id

        Args:
            crm_org_id: EspoCRM Account UUID string.

        Returns:
            Raw account dict.
        """
        return await self._get(f"/api/v1/Account/{crm_org_id}")

    # ------------------------------------------------------------------
    # Comments (Case stream) endpoints
    # ------------------------------------------------------------------
    async def get_comments_by_ticket(self, crm_ticket_id: str) -> list[dict]:
        all_posts: list[dict] = []
        offset = 0
        while True:
            response = await self._get(
                f"/api/v1/Case/{crm_ticket_id}/stream",
                params={
                    "offset":               offset,
                    "maxSize":              ESPO_PAGE_SIZE,
                    "where[0][type]":       "equals",
                    "where[0][attribute]":  "type",
                    "where[0][value]":      "Post",
                },
            )
            batch = response.get("list", [])
            total = response.get("total", 0)
            if not batch:
                break
            all_posts.extend(batch)
            offset += ESPO_PAGE_SIZE
            if len(all_posts) >= total:
                break
        logger.debug(
            "EspoCRM: fetched %d stream posts for Case %s", len(all_posts), crm_ticket_id
        )
        return all_posts

    async def post_comment(
        self,
        crm_ticket_id: str,
        body: str,
        author_name: str,
    ) -> dict:
        payload = {
            "type":       "Post",
            "parentId":   crm_ticket_id,
            "parentType": "Case",
            "post":       body,
        }
        return await self._post("/api/v1/Note", payload)