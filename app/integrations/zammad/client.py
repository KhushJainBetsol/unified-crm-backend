"""
app/integrations/zammad/client.py

Low-level async HTTP client for the Zammad REST API.

Responsibilities:
  - Authentication via Bearer token
  - Raw HTTP requests to Zammad endpoints
  - HTTP error handling and retries
  - Pagination handling for list endpoints

This class knows nothing about your DB models or normalisation.
It only fetches raw JSON from Zammad and returns it as dicts.

Zammad API docs: https://docs.zammad.org/en/latest/api/intro.html
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Zammad returns max 100 tickets per page
ZAMMAD_PAGE_SIZE = 100


class ZammadClientError(Exception):
    """Raised when Zammad API returns an error response."""
    pass


class ZammadAuthError(ZammadClientError):
    """Raised on 401 / 403 responses."""
    pass


class ZammadClient:
    """
    Async HTTP client for Zammad REST API.

    Usage:
        async with ZammadClient() as client:
            tickets = await client.get_tickets()

    Or inject via FastAPI dependency (see dependencies/).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self._base_url  = (base_url or settings.ZAMMAD_BASE_URL).rstrip("/")
        self._api_token = api_token or settings.ZAMMAD_API_TOKEN
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager — ensures client is always closed
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "ZammadClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Token token={self._api_token}",
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
                "ZammadClient must be used as an async context manager: "
                "`async with ZammadClient() as client:`"
            )
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """
        Perform a GET request and return the parsed JSON response.

        Raises:
            ZammadAuthError:   on 401 / 403
            ZammadClientError: on any other non-2xx response
        """
        client = self._ensure_client()
        logger.debug("Zammad GET %s params=%s", path, params)

        response = await client.get(path, params=params)

        if response.status_code in (401, 403):
            raise ZammadAuthError(
                f"Zammad authentication failed ({response.status_code}). "
                "Check ZAMMAD_API_TOKEN in your .env"
            )
        if not response.is_success:
            raise ZammadClientError(
                f"Zammad API error {response.status_code} for GET {path}: "
                f"{response.text[:300]}"
            )

        return response.json()

    async def _put(self, path: str, data: dict) -> Any:
        client = self._ensure_client()
        logger.debug("Zammad PUT %s payload=%s", path, data)
        response = await client.put(path, json=data)
        if response.status_code in (401, 403):
            raise ZammadAuthError(
                f"Zammad authentication failed ({response.status_code}). "
                "Check ZAMMAD_API_TOKEN in your .env"
            )
        if not response.is_success:
            raise ZammadClientError(
                f"Zammad API error {response.status_code} for PUT {path}: "
                f"{response.text[:300]}"
            )
        return response.json()

    async def _post(self, path: str, data: dict) -> Any:
        client = self._ensure_client()
        logger.debug("Zammad POST %s", path)
        response = await client.post(path, json=data)
        if response.status_code in (401, 403):
            raise ZammadAuthError(
                f"Zammad authentication failed ({response.status_code}). "
                "Check ZAMMAD_API_TOKEN in your .env"
            )
        if not response.is_success:
            raise ZammadClientError(
                f"Zammad API error {response.status_code} for POST {path}: "
                f"{response.text[:300]}"
            )
        return response.json()

    # ------------------------------------------------------------------
    # Ticket endpoints
    # ------------------------------------------------------------------
    async def get_ticket_by_id(self, ticket_id: int | str) -> dict:
        return await self._get(f"/api/v1/tickets/{ticket_id}")

    async def get_tickets(
        self,
        page: int = 1,
        per_page: int = ZAMMAD_PAGE_SIZE,
    ) -> list[dict]:
        return await self._get(
            "/api/v1/tickets",
            params={"page": page, "per_page": min(per_page, ZAMMAD_PAGE_SIZE)},
        )

    async def get_all_tickets(
        self,
        per_page: int = ZAMMAD_PAGE_SIZE,
    ) -> list[dict]:
        all_tickets: list[dict] = []
        page = 1
        while True:
            logger.info("Fetching Zammad tickets page %d", page)
            batch = await self.get_tickets(page=page, per_page=per_page)
            if not batch:
                break
            all_tickets.extend(batch)
            logger.info("Fetched %d tickets (total so far: %d)", len(batch), len(all_tickets))
            if len(batch) < per_page:
                break
            page += 1
        logger.info("Zammad: fetched %d tickets total", len(all_tickets))
        return all_tickets

    async def get_tickets_by_org(
        self,
        crm_org_id: str,
        per_page: int = ZAMMAD_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch ALL tickets belonging to a specific Zammad organization.

        Uses the search endpoint with query: organization_id:<crm_org_id>
        GET /api/v1/tickets/search?query=organization_id:<id>&per_page=100&page=N

        Args:
            crm_org_id: Zammad organization integer ID (stored as string).
            per_page:   Tickets per page (max 100).

        Returns:
            Flat list of all raw ticket dicts for that organization.
        """
        all_tickets: list[dict] = []
        page = 1

        while True:
            logger.info(
                "Fetching Zammad tickets for org_id=%s page=%d", crm_org_id, page
            )
            response = await self._get(
                "/api/v1/tickets/search",
                params={
                    "query":    f"organization_id:{crm_org_id}",
                    "per_page": min(per_page, ZAMMAD_PAGE_SIZE),
                    "page":     page,
                },
            )
            # Search endpoint returns a list directly (not wrapped)
            batch: list[dict] = response if isinstance(response, list) else response.get("assets", {}).get("Ticket", {})
            # Normalize: search may return a dict of {id: ticket} or a plain list
            if isinstance(batch, dict):
                batch = list(batch.values())

            if not batch:
                break

            all_tickets.extend(batch)
            logger.info(
                "Zammad org=%s: fetched %d tickets (total so far: %d)",
                crm_org_id, len(batch), len(all_tickets),
            )
            if len(batch) < per_page:
                break
            page += 1

        logger.info(
            "Zammad: fetched %d tickets total for org_id=%s", len(all_tickets), crm_org_id
        )
        return all_tickets

    async def get_tickets_updated_since(self, since: str) -> list[dict]:
        logger.info("Fetching Zammad tickets updated since %s", since)
        return await self._get(
            "/api/v1/tickets/search",
            params={
                "query":    f"updated_at:>{since}",
                "per_page": ZAMMAD_PAGE_SIZE,
            },
        )

    async def update_ticket(self, crm_ticket_id: str | int, data: dict) -> dict:
        return await self._put(f"/api/v1/tickets/{crm_ticket_id}", data)

    async def get_ticket_field_options(self) -> dict[str, list[str]]:
        states_response     = await self._get("/api/v1/ticket_states")
        priorities_response = await self._get("/api/v1/ticket_priorities")
        valid_states = [
            s["name"].lower()
            for s in states_response
            if s.get("active", True)
        ]
        valid_priorities = [
            p["name"]
            for p in priorities_response
            if p.get("active", True)
        ]
        return {"state": valid_states, "priority": valid_priorities}

    # ------------------------------------------------------------------
    # Comments (article) endpoints
    # ------------------------------------------------------------------
    async def get_comments_by_ticket(self, crm_ticket_id: str | int) -> list[dict]:
        path = f"/api/v1/ticket_articles/by_ticket/{crm_ticket_id}"
        logger.debug("Fetching Zammad articles for ticket %s", crm_ticket_id)
        response = await self._get(path)
        if isinstance(response, list):
            return response
        return response.get("ticket_articles", response.get("articles", []))

    async def post_comment(
        self,
        crm_ticket_id: str,
        body: str,
        author_name: str,
    ) -> dict:
        try:
            ticket_id = int(crm_ticket_id)
        except (ValueError, TypeError):
            raise ZammadClientError(
                f"crm_ticket_id must be numeric for Zammad, got: {crm_ticket_id!r}. "
                "This ticket may not have been synced from Zammad correctly."
            )
        payload = {
            "ticket_id":    ticket_id,
            "body":         body,
            "content_type": "text/plain",
            "type":         "note",
            "internal":     False,
        }
        return await self._post("/api/v1/ticket_articles", payload)

    # ------------------------------------------------------------------
    # Internal helper — fetch ALL users (paginated, no role filter)
    # ------------------------------------------------------------------
    async def _get_all_users(self) -> list[dict]:
        """
        Fetch every user from Zammad with full pagination.

        Zammad role_ids (seeded defaults):
          1 = Admin
          2 = Agent
          3 = Customer
        """
        all_users: list[dict] = []
        page = 1
        while True:
            logger.info("Fetching Zammad users page %d", page)
            batch = await self._get(
                "/api/v1/users",
                params={"page": page, "per_page": ZAMMAD_PAGE_SIZE},
            )
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < ZAMMAD_PAGE_SIZE:
                break
            page += 1
        logger.info("Zammad: fetched %d users total", len(all_users))
        return all_users

    async def get_users_by_org(self, crm_org_id: str) -> list[dict]:
        """
        Fetch all Zammad users belonging to a specific organization.

        GET /api/v1/users/search?query=organization_id:<crm_org_id>

        Args:
            crm_org_id: Zammad organization integer ID (stored as string).

        Returns:
            Flat list of raw user dicts for that organization.
        """
        all_users: list[dict] = []
        page = 1

        while True:
            logger.info(
                "Fetching Zammad users for org_id=%s page=%d", crm_org_id, page
            )
            response = await self._get(
                "/api/v1/users/search",
                params={
                    "query":    f"organization_id:{crm_org_id}",
                    "per_page": ZAMMAD_PAGE_SIZE,
                    "page":     page,
                },
            )
            batch: list[dict] = response if isinstance(response, list) else []
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < ZAMMAD_PAGE_SIZE:
                break
            page += 1

        logger.info(
            "Zammad: fetched %d users for org_id=%s", len(all_users), crm_org_id
        )
        return all_users

    # ------------------------------------------------------------------
    # Agent endpoints
    # ------------------------------------------------------------------
    async def get_all_agents(self) -> list[dict]:
        AGENT_ROLE_ID = 2
        all_users = await self._get_all_users()
        agents = [
            u for u in all_users
            if u.get("id") != 1 and AGENT_ROLE_ID in (u.get("role_ids") or [])
        ]
        logger.info("Zammad: filtered %d agents from %d total users", len(agents), len(all_users))
        return agents

    async def get_agents_by_org(self, crm_org_id: str) -> list[dict]:
        """
        Return agents (role_id=2) who belong to the given Zammad organization.

        Args:
            crm_org_id: Zammad organization integer ID (stored as string).
        """
        AGENT_ROLE_ID = 2
        users = await self.get_users_by_org(crm_org_id)
        agents = [
            u for u in users
            if u.get("id") != 1 and AGENT_ROLE_ID in (u.get("role_ids") or [])
        ]
        logger.info(
            "Zammad: filtered %d agents for org_id=%s", len(agents), crm_org_id
        )
        return agents

    # ------------------------------------------------------------------
    # Customer endpoints
    # ------------------------------------------------------------------
    async def get_all_customers(self) -> list[dict]:
        CUSTOMER_ROLE_ID = 3
        all_users = await self._get_all_users()
        customers = [
            u for u in all_users
            if u.get("id") != 1 and CUSTOMER_ROLE_ID in (u.get("role_ids") or [])
        ]
        logger.info("Zammad: filtered %d customers from %d total users", len(customers), len(all_users))
        return customers

    async def get_customers_by_org(self, crm_org_id: str) -> list[dict]:
        """
        Return customers (role_id=3) who belong to the given Zammad organization.

        Args:
            crm_org_id: Zammad organization integer ID (stored as string).
        """
        CUSTOMER_ROLE_ID = 3
        users = await self.get_users_by_org(crm_org_id)
        customers = [
            u for u in users
            if u.get("id") != 1 and CUSTOMER_ROLE_ID in (u.get("role_ids") or [])
        ]
        logger.info(
            "Zammad: filtered %d customers for org_id=%s", len(customers), crm_org_id
        )
        return customers

    # ------------------------------------------------------------------
    # Organization endpoints
    # ------------------------------------------------------------------
    async def get_all_organizations(self) -> list[dict]:
        all_orgs: list[dict] = []
        page = 1
        while True:
            logger.info("Fetching Zammad organizations page %d", page)
            batch = await self._get(
                "/api/v1/organizations",
                params={"page": page, "per_page": ZAMMAD_PAGE_SIZE},
            )
            if not batch:
                break
            all_orgs.extend(batch)
            if len(batch) < ZAMMAD_PAGE_SIZE:
                break
            page += 1
        logger.info("Zammad: fetched %d organizations total", len(all_orgs))
        return all_orgs

    async def get_organization_by_id(self, crm_org_id: str) -> dict:
        """
        Fetch a single Zammad organization by its ID.

        GET /api/v1/organizations/:id

        Args:
            crm_org_id: Zammad organization integer ID (stored as string).

        Returns:
            Raw organization dict.
        """
        return await self._get(f"/api/v1/organizations/{crm_org_id}")