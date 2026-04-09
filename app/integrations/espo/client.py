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

Agent-scoping quirk:
  EspoCRM User records have no accountId FK — users are instance-level.
  Contact records DO have an accountId FK, and Contact.emailAddress
  matches User.emailAddress for the same person.

  IMPORTANT: Contact.id != User.id — they are completely different records.
  The only reliable bridge is the shared email address.

  get_agents_by_account() therefore:
    1. Fetches all Contacts for the account (already paginated)
    2. Collects the set of email addresses from those contacts
    3. For each email, queries:
         GET /api/v1/User?where[0][type]=equals
                         &where[0][attribute]=emailAddress
                         &where[0][value]=<email>
       to find the matching User record
    4. Filters returned Users to those whose title == "Agent"

  This ensures syncing TCS only pulls Rajnandini, not every agent
  in the entire EspoCRM instance.

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

    async def _get_user_by_email(self, email: str) -> dict | None:
        """
        Fetch a single EspoCRM User record by email address.

        GET /api/v1/User
          ?where[0][type]=equals
          &where[0][attribute]=emailAddress
          &where[0][value]=<email>

        This is the correct Contact → User bridge because:
          Contact.emailAddress == User.emailAddress  (same person)
          Contact.id           != User.id            (different records)

        Args:
            email: Email address to look up.

        Returns:
            First matching User dict, or None if not found / on error.
        """
        logger.debug("EspoCRM: looking up user by email=%s", email)
        try:
            response = await self._get(
                "/api/v1/User",
                params={
                    "where[0][type]":      "equals",
                    "where[0][attribute]": "emailAddress",
                    "where[0][value]":     email,
                    "maxSize":             1,
                },
            )
        except EspoClientError as exc:
            logger.warning(
                "EspoCRM: failed to look up user by email=%s: %s", email, exc
            )
            return None

        users: list[dict] = response.get("list", [])
        if not users:
            logger.debug("EspoCRM: no user found for email=%s", email)
            return None

        user = users[0]
        logger.debug(
            "EspoCRM: resolved user id=%s name=%s for email=%s",
            user.get("id"), user.get("name"), email,
        )
        return user

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
            if user.get("title") == AGENT_ROLE_KEYWORD
        ]
        logger.info("EspoCRM: fetched %d agents total", len(agents))
        return agents

    async def get_agents_by_account(self, crm_org_id: str) -> list[dict]:
        """
        Fetch EspoCRM users (agents) that belong to a specific Account.

        WHY THIS IS NON-TRIVIAL
        -----------------------
        EspoCRM User records carry no accountId FK — users are instance-level.
        The account relationship lives on the Contact entity: each Contact has
        an accountId pointing to its Account.

        CRITICAL: Contact.id != User.id — they are completely different records.
        The only reliable bridge is: Contact.emailAddress == User.emailAddress.

        STRATEGY
        --------
        1. Fetch all Contacts for this account via get_contacts_by_account.
        2. Collect the email addresses from those contacts.
        3. For each email, call:
             GET /api/v1/User?where[0][type]=equals
                             &where[0][attribute]=emailAddress
                             &where[0][value]=<email>
           to find the matching User record.
        4. Keep only users whose title == "Agent".

        Example: TCS account has contact Rajnandini (rajnandini@tcs.com).
        We look up the User with emailAddress=rajnandini@tcs.com, confirm
        their title is "Agent", and return only that user — not every agent
        in the EspoCRM instance.

        Args:
            crm_org_id: EspoCRM Account UUID string
                        (crm_org_id from tenant_source_systems).

        Returns:
            List of User dicts for agents linked to this account.
        """
        # Step 1: contacts for this account carry the email addresses we need
        contacts = await self.get_contacts_by_account(crm_org_id)

        if not contacts:
            logger.info(
                "EspoCRM: no contacts found for account_id=%s — no agents to sync",
                crm_org_id,
            )
            return []

        # Step 2: collect non-empty email addresses from contacts
        contact_emails: list[str] = [
            c["emailAddress"]
            for c in contacts
            if c.get("emailAddress")
        ]

        if not contact_emails:
            logger.warning(
                "EspoCRM: contacts found for account_id=%s but none have emailAddress — "
                "cannot resolve users",
                crm_org_id,
            )
            return []

        logger.info(
            "EspoCRM: account_id=%s — resolving %d contact email(s) to User records",
            crm_org_id, len(contact_emails),
        )

        # Step 3 & 4: look up each email → User, keep only agents
        agents: list[dict] = []
        for email in contact_emails:
            user = await self._get_user_by_email(email)
            if user is None:
                logger.warning(
                    "EspoCRM: no User found for contact email=%s (account_id=%s) — skipping",
                    email, crm_org_id,
                )
                continue

            title = user.get("title", "")
            if title == AGENT_ROLE_KEYWORD:
                agents.append(user)
                logger.debug(
                    "EspoCRM: agent resolved — id=%s name=%s email=%s",
                    user.get("id"), user.get("name"), email,
                )
            else:
                logger.debug(
                    "EspoCRM: user id=%s email=%s title=%r is not an agent — skipping",
                    user.get("id"), email, title,
                )

        logger.info(
            "EspoCRM: resolved %d agent(s) for account_id=%s from %d contact email(s)",
            len(agents), crm_org_id, len(contact_emails),
        )
        return agents

    # ------------------------------------------------------------------
    # Customer (Contact) endpoints
    # ------------------------------------------------------------------
    async def get_all_customers(self) -> list[dict]:
        all_users = await self._get_all_users_with_detail()
        customers = [
            user for user in all_users
            if user.get("title") == CUSTOMER_ROLE_KEYWORD
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

        Also used internally by get_agents_by_account:
          contact.emailAddress → GET /api/v1/User?emailAddress=<email>
        is the only reliable Contact → User bridge in EspoCRM
        (Contact.id != User.id).

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