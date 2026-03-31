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

Agent vs Customer differentiation strategy:
  - GET /api/v1/User (list) gives minimal fields — no rolesNames
  - GET /api/v1/User/:id (detail) includes rolesNames dict
  - _get_all_users_with_detail() fetches all IDs then hydrates each one
  - get_all_agents()    → calls _get_all_users_with_detail(), keeps rolesNames == "agent"
  - get_all_customers() → calls _get_all_users_with_detail(), keeps rolesNames == "customer"

EspoCRM API docs: https://docs.espocrm.com/development/api/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# EspoCRM recommends max 200 records per request
ESPO_PAGE_SIZE = 200

# Role name keyword for agents (case-insensitive match against rolesNames values)
AGENT_ROLE_KEYWORD = "Agent"

# Role name keyword for customers (case-insensitive match against rolesNames values)
CUSTOMER_ROLE_KEYWORD = "Customer"


class EspoClientError(Exception):
    """Raised when EspoCRM API returns an error response."""

    pass


class EspoAuthError(EspoClientError):
    """Raised on 401 / 403 responses."""

    pass


class EspoClient:
    """
    Async HTTP client for EspoCRM REST API.

    Usage:
        async with EspoClient() as client:
            tickets = await client.get_all_tickets()

    Or inject via FastAPI dependency.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ESPO_BASE_URL).rstrip("/")
        self._api_key = api_key or settings.ESPO_API_KEY
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "EspoClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-Api-Key": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "EspoClient must be used as an async context manager: "
                "`async with EspoClient() as client:`"
            )
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """
        Perform a GET request and return the parsed JSON response.

        Raises:
            EspoAuthError: on 401 / 403
            EspoClientError: on any other non-2xx response
        """
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
        """
        Perform a PUT request and return the parsed JSON response.

        Raises:
            EspoAuthError: on 401 / 403
            EspoClientError: on any other non-2xx response
        """
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

    async def get_case_field_options(self) -> dict[str, list[str]]:
        """
        Fetch the valid field options for the Case entity from EspoCRM metadata.

        Calls GET /api/v1/metadata?scopes[]=Case and extracts the `options`
        list for each field — this gives us the exact enum values this
        EspoCRM instance accepts for status, priority, etc.

        EspoCRM allows admins to add/rename/remove options via the admin UI,
        so the valid values vary per instance and cannot be hardcoded safely.

        Returns:
            Dict mapping field name → list of valid option strings.
            e.g. {
                "status":   ["New", "Assigned", "Pending Input", "Closed", ...],
                "priority": ["Low", "Normal", "High", "Urgent"],
            }
        """
        response = await self._get(
            "/api/v1/metadata",
            params={"scopes[]": "Case"},
        )

        # Response shape:
        # {
        #   "entityDefs": {
        #     "Case": {
        #       "fields": {
        #         "status":   { "type": "enum", "options": ["New", "Assigned", ...] },
        #         "priority": { "type": "enum", "options": ["Low", "Normal", ...] },
        #         ...
        #       }
        #     }
        #   }
        # }
        fields: dict = response.get("entityDefs", {}).get("Case", {}).get("fields", {})

        return {
            field_name: field_def.get("options", [])
            for field_name, field_def in fields.items()
            if field_def.get("options")  # only fields that have an enum options list
        }

    async def _get_all_users_with_detail(self) -> list[dict]:
        """
        Shared helper used by get_all_agents() and get_all_customers().

        Step 1 — paginate GET /api/v1/User to collect all user IDs.
                 (List endpoint returns minimal fields, no rolesNames.)
        Step 2 — fetch GET /api/v1/User/:id for every ID to get the full
                 detail record, which includes rolesNames, teamsNames,
                 lastAccess, etc.

        Returns:
            List of fully-hydrated raw user dicts.
        """
        all_ids: list[str] = []
        offset = 0

        while True:
            response = await self._get(
                "/api/v1/User",
                params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
            )
            batch: list[dict] = response.get("list", [])
            total: int = response.get("total", 0)

            if not batch:
                break

            all_ids.extend(user["id"] for user in batch)
            offset += ESPO_PAGE_SIZE

            if len(all_ids) >= total:
                break

        logger.info(
            "EspoCRM: found %d user IDs, fetching full details...", len(all_ids)
        )

        detailed_users: list[dict] = []

        for i, user_id in enumerate(all_ids, start=1):
            logger.debug("Fetching user detail %d/%d  id=%s", i, len(all_ids), user_id)
            try:
                detail = await self._get(f"/api/v1/User/{user_id}")
                detailed_users.append(detail)
            except EspoClientError as exc:
                logger.warning(
                    "Skipping user %s — failed to fetch detail: %s", user_id, exc
                )

        logger.info(
            "EspoCRM: fetched full details for %d / %d users",
            len(detailed_users),
            len(all_ids),
        )
        return detailed_users

    # ------------------------------------------------------------------
    # Ticket (Case) endpoints
    # ------------------------------------------------------------------
    async def get_ticket_by_id(self, ticket_id: str) -> dict:
        """
        Fetch a single Case by its EspoCRM ID.

        GET /api/v1/Case/:id

        Args:
            ticket_id: EspoCRM Case string UUID.

        Returns:
            Raw case dict from EspoCRM API.
        """
        return await self._get(f"/api/v1/Case/{ticket_id}")

    async def get_tickets(
        self,
        offset: int = 0,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> tuple[list[dict], int]:
        """
        Fetch a single page of Cases from EspoCRM.

        GET /api/v1/Case?offset=0&maxSize=200

        Args:
            offset:   Number of records to skip (0-indexed).
            max_size: Records per request (max 200).

        Returns:
            Tuple of (list of raw case dicts, total record count).
        """
        response = await self._get(
            "/api/v1/Case",
            params={
                "offset": offset,
                "maxSize": min(max_size, ESPO_PAGE_SIZE),
                "orderBy": "createdAt",
                "order": "asc",
            },
        )
        return response.get("list", []), response.get("total", 0)

    async def get_all_tickets(
        self,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch ALL Cases by paginating through every page automatically.

        Args:
            max_size: Records per request (max 200).

        Returns:
            Flat list of all raw case dicts.
        """
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
            logger.info(
                "EspoCRM: fetched %d tickets (total so far: %d / %d)",
                len(batch),
                len(all_tickets),
                total,
            )

        logger.info("EspoCRM: fetched %d tickets total", len(all_tickets))
        return all_tickets

    async def get_tickets_updated_since(self, since: str) -> list[dict]:
        """
        Fetch Cases modified after a given timestamp.
        Used for incremental sync.

        Args:
            since: ISO 8601 datetime string e.g. "2024-01-01T00:00:00Z"

        Returns:
            List of raw case dicts modified after `since`.
        """
        logger.info("Fetching EspoCRM cases updated since %s", since)
        all_tickets: list[dict] = []
        offset = 0

        while True:
            response = await self._get(
                "/api/v1/Case",
                params={
                    "offset": offset,
                    "maxSize": ESPO_PAGE_SIZE,
                    "orderBy": "modifiedAt",
                    "order": "asc",
                    "where[0][type]": "after",
                    "where[0][attribute]": "modifiedAt",
                    "where[0][value]": since,
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
        """
        Update an existing EspoCRM Case.

        PUT /api/v1/Case/:id

        Accepted fields (send only what changed):
            status           → e.g. "Assigned", "Pending Input", "Closed"
            priority         → e.g. "Low", "Normal", "High", "Urgent"
            assignedUserId   → EspoCRM string UUID of the assigned agent

        Args:
            crm_ticket_id: EspoCRM Case UUID string.
            data:          Dict of fields to update (EspoCRM field names).

        Returns:
            Raw updated Case dict from EspoCRM.
        """
        return await self._put(f"/api/v1/Case/{crm_ticket_id}", data)

    # ------------------------------------------------------------------
    # Agent (User) endpoints
    # ------------------------------------------------------------------
    async def get_all_agents(self) -> list[dict]:
        """
        Fetch all EspoCRM users whose rolesNames contains "agent".

        Returns:
            Flat list of full user dicts for agents only.
        """
        all_users = await self._get_all_users_with_detail()

        agents = [
            user
            for user in all_users
            if any(
                AGENT_ROLE_KEYWORD in role_name
                for role_name in (user.get("rolesNames") or {}).values()
            )
        ]

        logger.info("EspoCRM: fetched %d agents total", len(agents))
        return agents

    async def _get_users(self, offset: int = 0) -> tuple[list[dict], int]:
        response = await self._get(
            "/api/v1/User",
            params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
        )
        return response.get("list", []), response.get("total", 0)

    # ------------------------------------------------------------------
    # Customer (Contact) endpoints
    # ------------------------------------------------------------------
    async def get_all_customers(self) -> list[dict]:
        """
        Fetch all EspoCRM users whose rolesNames contains "customer".

        Returns:
            Flat list of full user dicts for customers only.
        """
        all_users = await self._get_all_users_with_detail()

        customers = [
            user
            for user in all_users
            if any(
                CUSTOMER_ROLE_KEYWORD in role_name
                for role_name in (user.get("rolesNames") or {}).values()
            )
        ]

        logger.info("EspoCRM: fetched %d customers total", len(customers))
        return customers

    async def _get_contacts(self, offset: int = 0) -> tuple[list[dict], int]:
        response = await self._get(
            "/api/v1/Contact",
            params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
        )
        return response.get("list", []), response.get("total", 0)

    # ------------------------------------------------------------------
    # Company (Account) endpoints
    # ------------------------------------------------------------------
    async def get_all_companies(self) -> list[dict]:
        """
        Fetch all EspoCRM accounts (companies).
        GET /api/v1/Account
        Returns flat list of raw account dicts.
        """
        all_companies: list[dict] = []
        offset = 0

        batch, total = await self._get_accounts(offset=offset)
        all_companies.extend(batch)

        while len(all_companies) < total:
            offset += ESPO_PAGE_SIZE
            batch, _ = await self._get_accounts(offset=offset)
            if not batch:
                break
            all_companies.extend(batch)

        logger.info("EspoCRM: fetched %d companies total", len(all_companies))
        return all_companies

    async def _get_accounts(self, offset: int = 0) -> tuple[list[dict], int]:
        response = await self._get(
            "/api/v1/Account",
            params={"offset": offset, "maxSize": ESPO_PAGE_SIZE},
        )
        return response.get("list", []), response.get("total", 0)

    # ------------------------------------------------------------------
    # Comments (Case stream) endpoints
    # ------------------------------------------------------------------
    async def get_comments_by_ticket(self, crm_ticket_id: str) -> list[dict]:
        """
        Fetch all stream Post items for an EspoCRM Case (ticket comments).

        GET /api/v1/Case/{id}/stream
            ?where[0][type]=equals
            &where[0][attribute]=type
            &where[0][value]=Post

        Args:
            crm_ticket_id: EspoCRM Case UUID string.

        Returns:
            List of raw stream Post dicts.
        """
        all_posts: list[dict] = []
        offset = 0

        while True:
            response = await self._get(
                f"/api/v1/Case/{crm_ticket_id}/stream",
                params={
                    "offset": offset,
                    "maxSize": ESPO_PAGE_SIZE,
                    "where[0][type]": "equals",
                    "where[0][attribute]": "type",
                    "where[0][value]": "Post",
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
            "EspoCRM: fetched %d stream posts for Case %s",
            len(all_posts),
            crm_ticket_id,
        )
        return all_posts
