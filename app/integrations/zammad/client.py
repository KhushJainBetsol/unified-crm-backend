# """
# app/integrations/zammad/client.py

# Low-level async HTTP client for the Zammad REST API.

# Responsibilities:
#   - Authentication via Bearer token
#   - Raw HTTP requests to Zammad endpoints
#   - HTTP error handling and retries
#   - Pagination handling for list endpoints

# This class knows nothing about your DB models or normalisation.
# It only fetches raw JSON from Zammad and returns it as dicts.

# Zammad API docs: https://docs.zammad.org/en/latest/api/intro.html
# """

# from __future__ import annotations

# import logging
# from typing import Any

# import httpx

# from app.core.settings import get_settings

# logger = logging.getLogger(__name__)

# settings = get_settings()

# # Zammad returns max 100 tickets per page
# ZAMMAD_PAGE_SIZE = 100


# class ZammadClientError(Exception):
#     """Raised when Zammad API returns an error response."""
#     pass


# class ZammadAuthError(ZammadClientError):
#     """Raised on 401 / 403 responses."""
#     pass


# class ZammadClient:
#     """
#     Async HTTP client for Zammad REST API.

#     Usage:
#         async with ZammadClient() as client:
#             tickets = await client.get_tickets()

#     Or inject via FastAPI dependency (see dependencies/).
#     """

#     def __init__(
#         self,
#         base_url: str | None = None,
#         api_token: str | None = None,
#     ) -> None:
#         self._base_url = (base_url or settings.ZAMMAD_BASE_URL).rstrip("/")
#         self._api_token = api_token or settings.ZAMMAD_API_TOKEN
#         self._client: httpx.AsyncClient | None = None

#     # ------------------------------------------------------------------
#     # Context manager — ensures client is always closed
#     # ------------------------------------------------------------------
#     async def __aenter__(self) -> "ZammadClient":
#         self._client = httpx.AsyncClient(
#             base_url=self._base_url,
#             headers={
#                 "Authorization": f"Token token={self._api_token}",
#                 "Content-Type": "application/json",
#             },
#             timeout=httpx.Timeout(30.0),
#         )
#         return self

#     async def __aexit__(self, *args: Any) -> None:
#         if self._client:
#             await self._client.aclose()

#     # ------------------------------------------------------------------
#     # Internal helpers
#     # ------------------------------------------------------------------
#     def _ensure_client(self) -> httpx.AsyncClient:
#         if self._client is None:
#             raise RuntimeError(
#                 "ZammadClient must be used as an async context manager: "
#                 "`async with ZammadClient() as client:`"
#             )
#         return self._client

#     async def _get(self, path: str, params: dict | None = None) -> Any:
#         """
#         Perform a GET request and return the parsed JSON response.

#         Raises:
#             ZammadAuthError: on 401 / 403
#             ZammadClientError: on any other non-2xx response
#         """
#         client = self._ensure_client()
#         logger.debug("Zammad GET %s params=%s", path, params)

#         response = await client.get(path, params=params)

#         if response.status_code in (401, 403):
#             raise ZammadAuthError(
#                 f"Zammad authentication failed ({response.status_code}). "
#                 "Check ZAMMAD_API_TOKEN in your .env"
#             )
#         if not response.is_success:
#             raise ZammadClientError(
#                 f"Zammad API error {response.status_code} for GET {path}: "
#                 f"{response.text[:300]}"
#             )

#         return response.json()

#     # ------------------------------------------------------------------
#     # Ticket endpoints
#     # ------------------------------------------------------------------
#     async def get_ticket_by_id(self, ticket_id: int | str) -> dict:
#         """
#         Fetch a single ticket by its Zammad ID.

#         GET /api/v1/tickets/:id

#         Args:
#             ticket_id: Zammad ticket integer ID.

#         Returns:
#             Raw ticket dict from Zammad API.
#         """
#         return await self._get(f"/api/v1/tickets/{ticket_id}")

#     async def get_tickets(
#         self,
#         page: int = 1,
#         per_page: int = ZAMMAD_PAGE_SIZE,
#     ) -> list[dict]:
#         """
#         Fetch a single page of tickets.

#         GET /api/v1/tickets?page=1&per_page=100

#         Args:
#             page:     Page number (1-indexed).
#             per_page: Tickets per page (max 100).

#         Returns:
#             List of raw ticket dicts.
#         """
#         return await self._get(
#             "/api/v1/tickets",
#             params={"page": page, "per_page": min(per_page, ZAMMAD_PAGE_SIZE)},
#         )

#     async def get_all_tickets(
#         self,
#         per_page: int = ZAMMAD_PAGE_SIZE,
#     ) -> list[dict]:
#         """
#         Fetch ALL tickets by paginating through every page automatically.

#         Stops when a page returns fewer results than per_page
#         (signals last page).

#         Args:
#             per_page: Tickets per page (max 100).

#         Returns:
#             Flat list of all raw ticket dicts.
#         """
#         all_tickets: list[dict] = []
#         page = 1

#         while True:
#             logger.info("Fetching Zammad tickets page %d", page)
#             batch = await self.get_tickets(page=page, per_page=per_page)

#             if not batch:
#                 break

#             all_tickets.extend(batch)
#             logger.info(
#                 "Fetched %d tickets (total so far: %d)",
#                 len(batch),
#                 len(all_tickets),
#             )

#             # last page — fewer results than requested means no more pages
#             if len(batch) < per_page:
#                 break

#             page += 1

#         logger.info("Zammad: fetched %d tickets total", len(all_tickets))
#         return all_tickets

#     async def get_tickets_updated_since(self, since: str) -> list[dict]:
#         """
#         Fetch tickets updated after a given timestamp.
#         Used for incremental sync to avoid re-fetching everything.

#         Zammad search endpoint: GET /api/v1/tickets/search

#         Args:
#             since: ISO 8601 datetime string e.g. "2024-01-01T00:00:00Z"

#         Returns:
#             List of raw ticket dicts updated after `since`.
#         """
#         logger.info("Fetching Zammad tickets updated since %s", since)
#         return await self._get(
#             "/api/v1/tickets/search",
#             params={
#                 "query": f"updated_at:>{since}",
#                 "per_page": ZAMMAD_PAGE_SIZE,
#             },
#         )

#     # ------------------------------------------------------------------
#     # Agent (User) endpoints
#     # ------------------------------------------------------------------
#     async def get_all_agents(self) -> list[dict]:
#         """
#         Fetch all Zammad users who are agents.
#         GET /api/v1/users?role=Agent
#         Returns flat list of raw user dicts.
#         """
#         all_agents: list[dict] = []
#         page = 1

#         while True:
#             logger.info("Fetching Zammad agents page %d", page)
#             batch = await self._get(
#                 "/api/v1/users",
#                 params={"page": page, "per_page": ZAMMAD_PAGE_SIZE, "role": "Agent"},
#             )
#             if not batch:
#                 break
#             all_agents.extend(batch)
#             if len(batch) < ZAMMAD_PAGE_SIZE:
#                 break
#             page += 1

#         logger.info("Zammad: fetched %d agents total", len(all_agents))
#         return all_agents

#     # ------------------------------------------------------------------
#     # Customer (User) endpoints
#     # ------------------------------------------------------------------
#     async def get_all_customers(self) -> list[dict]:
#         """
#         Fetch all Zammad users who are customers.
#         GET /api/v1/users?role=Customer
#         Returns flat list of raw user dicts.
#         """
#         all_customers: list[dict] = []
#         page = 1

#         while True:
#             logger.info("Fetching Zammad customers page %d", page)
#             batch = await self._get(
#                 "/api/v1/users",
#                 params={"page": page, "per_page": ZAMMAD_PAGE_SIZE, "role": "Customer"},
#             )
#             if not batch:
#                 break
#             all_customers.extend(batch)
#             if len(batch) < ZAMMAD_PAGE_SIZE:
#                 break
#             page += 1

#         logger.info("Zammad: fetched %d customers total", len(all_customers))
#         return all_customers

#     # ------------------------------------------------------------------
#     # Organization endpoints
#     # ------------------------------------------------------------------
#     async def get_all_organizations(self) -> list[dict]:
#         """
#         Fetch all Zammad organizations (companies).
#         GET /api/v1/organizations
#         Returns flat list of raw organization dicts.
#         """
#         all_orgs: list[dict] = []
#         page = 1

#         while True:
#             logger.info("Fetching Zammad organizations page %d", page)
#             batch = await self._get(
#                 "/api/v1/organizations",
#                 params={"page": page, "per_page": ZAMMAD_PAGE_SIZE},
#             )
#             if not batch:
#                 break
#             all_orgs.extend(batch)
#             if len(batch) < ZAMMAD_PAGE_SIZE:
#                 break
#             page += 1

#         logger.info("Zammad: fetched %d organizations total", len(all_orgs))
#         return all_orgs
    
#     async def get_comments_by_ticket(self, crm_ticket_id: str | int) -> list[dict]:
#         """
#         Fetch all articles (comments) for a Zammad ticket.

#         GET /api/v1/ticket_articles/by_ticket/{ticket_id}

#         Zammad returns ALL articles for a ticket in one response (no pagination).

#         Args:
#             crm_ticket_id: Zammad's integer ticket ID (e.g. 6).

#         Returns:
#             List of raw ticket_article dicts.

#         Example response item:
#             {
#                 "id": 12,
#                 "ticket_id": 6,
#                 "type": "note",
#                 "body": "Comment text here",
#                 "from": "Agent <agent@company.com>",
#                 "internal": false,
#                 "created_at": "2024-01-15T10:30:00.000Z",
#                 "updated_at": "2024-01-15T10:30:00.000Z"
#             }
#         """
#         path = f"/api/v1/ticket_articles/by_ticket/{crm_ticket_id}"
#         logger.debug("Fetching Zammad articles for ticket %s", crm_ticket_id)
#         response = await self._get(path)

#         # Zammad returns either a list directly or {"ticket_articles": [...]}
#         if isinstance(response, list):
#             return response
#         return response.get("ticket_articles", response.get("articles", []))


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
        self._base_url = (base_url or settings.ZAMMAD_BASE_URL).rstrip("/")
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
            ZammadAuthError: on 401 / 403
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

    # ------------------------------------------------------------------
    # Ticket endpoints
    # ------------------------------------------------------------------
    async def get_ticket_by_id(self, ticket_id: int | str) -> dict:
        """
        Fetch a single ticket by its Zammad ID.

        GET /api/v1/tickets/:id

        Args:
            ticket_id: Zammad ticket integer ID.

        Returns:
            Raw ticket dict from Zammad API.
        """
        return await self._get(f"/api/v1/tickets/{ticket_id}")

    async def get_tickets(
        self,
        page: int = 1,
        per_page: int = ZAMMAD_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch a single page of tickets.

        GET /api/v1/tickets?page=1&per_page=100

        Args:
            page:     Page number (1-indexed).
            per_page: Tickets per page (max 100).

        Returns:
            List of raw ticket dicts.
        """
        return await self._get(
            "/api/v1/tickets",
            params={"page": page, "per_page": min(per_page, ZAMMAD_PAGE_SIZE)},
        )

    async def get_all_tickets(
        self,
        per_page: int = ZAMMAD_PAGE_SIZE,
    ) -> list[dict]:
        """
        Fetch ALL tickets by paginating through every page automatically.

        Stops when a page returns fewer results than per_page
        (signals last page).

        Args:
            per_page: Tickets per page (max 100).

        Returns:
            Flat list of all raw ticket dicts.
        """
        all_tickets: list[dict] = []
        page = 1

        while True:
            logger.info("Fetching Zammad tickets page %d", page)
            batch = await self.get_tickets(page=page, per_page=per_page)

            if not batch:
                break

            all_tickets.extend(batch)
            logger.info(
                "Fetched %d tickets (total so far: %d)",
                len(batch),
                len(all_tickets),
            )

            # last page — fewer results than requested means no more pages
            if len(batch) < per_page:
                break

            page += 1

        logger.info("Zammad: fetched %d tickets total", len(all_tickets))
        return all_tickets

    async def get_tickets_updated_since(self, since: str) -> list[dict]:
        """
        Fetch tickets updated after a given timestamp.
        Used for incremental sync to avoid re-fetching everything.

        Zammad search endpoint: GET /api/v1/tickets/search

        Args:
            since: ISO 8601 datetime string e.g. "2024-01-01T00:00:00Z"

        Returns:
            List of raw ticket dicts updated after `since`.
        """
        logger.info("Fetching Zammad tickets updated since %s", since)
        return await self._get(
            "/api/v1/tickets/search",
            params={
                "query": f"updated_at:>{since}",
                "per_page": ZAMMAD_PAGE_SIZE,
            },
        )

    # ------------------------------------------------------------------
    # Internal helper — fetch ALL users (paginated, no role filter)
    # ------------------------------------------------------------------
    async def _get_all_users(self) -> list[dict]:
        """
        Fetch every user from Zammad with full pagination.

        The Zammad API ignores the `role` query param — it always returns
        all users regardless. Role filtering must be done client-side
        using the `role_ids` field on each user.

        Zammad role_ids (seeded defaults):
          1 = Admin
          2 = Agent
          3 = Customer

        Returns flat list of all raw user dicts.
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

    # ------------------------------------------------------------------
    # Agent endpoints
    # ------------------------------------------------------------------
    async def get_all_agents(self) -> list[dict]:
        """
        Return all Zammad users who have role_id=2 (Agent).

        A user can have multiple roles (e.g. Admin+Agent) — anyone with
        role_id 2 in their role_ids list is treated as an agent.

        Skips system user id=1 (the anonymous "-" user with no roles).

        Returns flat list of raw user dicts.
        """
        AGENT_ROLE_ID = 2
        all_users = await self._get_all_users()

        agents = [
            u for u in all_users
            if u.get("id") != 1                          # skip system user
            and AGENT_ROLE_ID in (u.get("role_ids") or [])
        ]

        logger.info(
            "Zammad: filtered %d agents (role_id=%d) from %d total users",
            len(agents), AGENT_ROLE_ID, len(all_users),
        )
        return agents

    # ------------------------------------------------------------------
    # Customer endpoints
    # ------------------------------------------------------------------
    async def get_all_customers(self) -> list[dict]:
        """
        Return all Zammad users who are pure customers.

        Rule:
          - Has role_id=3 (Customer)
          - Does NOT have role_id=2 (Agent) — agents are not customers
          - Skips system user id=1

        This prevents agents who also have the Customer role from
        being synced into the customers table.

        Returns flat list of raw user dicts.
        """
        CUSTOMER_ROLE_ID = 3

        all_users = await self._get_all_users()

        customers = [
            u for u in all_users
            if u.get("id") != 1                               # skip system user
            and CUSTOMER_ROLE_ID in (u.get("role_ids") or [])  # has Customer role
        ]

        logger.info(
            "Zammad: filtered %d pure customers (role_id=%d, not role_id=%d) from %d total users",
            len(customers), CUSTOMER_ROLE_ID, AGENT_ROLE_ID, len(all_users),
        )
        return customers

    # ------------------------------------------------------------------
    # Organization endpoints
    # ------------------------------------------------------------------
    async def get_all_organizations(self) -> list[dict]:
        """
        Fetch all Zammad organizations (companies).
        GET /api/v1/organizations
        Returns flat list of raw organization dicts.
        """
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
    
    async def get_comments_by_ticket(self, crm_ticket_id: str | int) -> list[dict]:
        """
        Fetch all articles (comments) for a Zammad ticket.

        GET /api/v1/ticket_articles/by_ticket/{ticket_id}

        Zammad returns ALL articles for a ticket in one response (no pagination).

        Args:
            crm_ticket_id: Zammad's integer ticket ID (e.g. 6).

        Returns:
            List of raw ticket_article dicts.

        Example response item:
            {
                "id": 12,
                "ticket_id": 6,
                "type": "note",
                "body": "Comment text here",
                "from": "Agent <agent@company.com>",
                "internal": false,
                "created_at": "2024-01-15T10:30:00.000Z",
                "updated_at": "2024-01-15T10:30:00.000Z"
            }
        """
        path = f"/api/v1/ticket_articles/by_ticket/{crm_ticket_id}"
        logger.debug("Fetching Zammad articles for ticket %s", crm_ticket_id)
        response = await self._get(path)

        # Zammad returns either a list directly or {"ticket_articles": [...]}
        if isinstance(response, list):
            return response
        return response.get("ticket_articles", response.get("articles", []))