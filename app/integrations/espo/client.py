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

# EspoCRM recommends max 200 records per request
ESPO_PAGE_SIZE = 200


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
                "X-Api-Key": self._api_key,    # EspoCRM uses X-Api-Key
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

        EspoCRM list response shape:
            { "list": [...], "total": 1234 }

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
        # EspoCRM wraps list responses: { "list": [...], "total": N }
        return response.get("list", []), response.get("total", 0)

    async def get_all_tickets(
        self,
        max_size: int = ESPO_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch ALL Cases by paginating through every page automatically.

        Uses offset-based pagination until all records are retrieved.

        Args:
            max_size: Records per request (max 200).

        Returns:
            Flat list of all raw case dicts.
        """
        all_tickets: list[dict] = []
        offset = 0

        # fetch first page to get total count
        batch, total = await self.get_tickets(offset=offset, max_size=max_size)
        all_tickets.extend(batch)
        logger.info(
            "EspoCRM: fetched %d tickets (total: %d)", len(all_tickets), total
        )

        # keep fetching until we have everything
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

        EspoCRM supports filtering via 'where' query parameter.

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

    # ------------------------------------------------------------------
    # Agent (User) endpoints
    # ------------------------------------------------------------------
    async def get_all_agents(self) -> list[dict]:
        """
        Fetch all EspoCRM users.
        GET /api/v1/User
        Returns flat list of raw user dicts.
        """
        all_agents: list[dict] = []
        offset = 0

        batch, total = await self._get_users(offset=offset)
        all_agents.extend(batch)

        while len(all_agents) < total:
            offset += ESPO_PAGE_SIZE
            batch, _ = await self._get_users(offset=offset)
            if not batch:
                break
            all_agents.extend(batch)

        logger.info("EspoCRM: fetched %d agents total", len(all_agents))
        return all_agents

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
        Fetch all EspoCRM contacts (customers).
        GET /api/v1/Contact
        Returns flat list of raw contact dicts.
        """
        all_customers: list[dict] = []
        offset = 0

        batch, total = await self._get_contacts(offset=offset)
        all_customers.extend(batch)

        while len(all_customers) < total:
            offset += ESPO_PAGE_SIZE
            batch, _ = await self._get_contacts(offset=offset)
            if not batch:
                break
            all_customers.extend(batch)

        logger.info("EspoCRM: fetched %d customers total", len(all_customers))
        return all_customers

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