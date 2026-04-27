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

Role detection:
  Agent/Customer roles are determined by inspecting the "rolesNames" dict
  on the full User detail record (GET /api/v1/User/{id}).

  rolesNames example:
    {"69b917db3281aa2e8": "Agent"}   → this user is an Agent
    {"69b917db3281aa2e8": "Customer"} → this user is a Customer

  This replaces the old approach of checking user.get("title").

Agent-scoping quirk:
  EspoCRM User records have no accountId FK — users are instance-level.
  Contact records DO have an accountId FK, and Contact.emailAddress
  matches User.emailAddress for the same person.

  IMPORTANT: Contact.id != User.id — they are completely different records.
  The only reliable bridge is the shared email address.

  get_agents_by_account() therefore:
    1. Fetches ALL users (GET /api/v1/User, then GET /api/v1/User/{id} each)
    2. Filters to users whose rolesNames values contain "Agent"
    3. Fetches all Contacts for the account (GET /api/v1/Contact?accountId=...)
    4. Cross-matches: keeps only agents whose emailAddress appears in the
       contact list for that account

  This ensures syncing TCS only pulls agents linked to TCS contacts,
  not every agent in the entire EspoCRM instance.

Customer fetching:
  get_all_customers() follows the same Steps 1-2 but filters by "Customer"
  in rolesNames — no account scoping needed.

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
        """
        Two-step fetch for all EspoCRM users:

        Step 1 — GET /api/v1/User (paginated)
            Returns lightweight user stubs: id, name, userName only.
            Paginate with offset/maxSize until len(ids) >= total.

        Step 2 — GET /api/v1/User/{id} for each user
            Returns the full user record including rolesNames, which is
            required to determine whether a user is an Agent or Customer.

        Returns:
            List of full user detail dicts.
        """
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
                logger.warning(
                    "Skipping user %s — failed to fetch detail: %s", user_id, exc
                )

        logger.info(
            "EspoCRM: fetched full details for %d / %d users",
            len(detailed_users), len(all_ids),
        )
        return detailed_users

    def _has_role(self, user: dict, role_keyword: str) -> bool:
        """
        Return True if the user's rolesNames dict contains role_keyword as a value.

        rolesNames is a dict mapping role ID → role name, e.g.:
            {"69b917db3281aa2e8": "Agent"}
            {"69b917db3281aa2e8": "Customer"}

        We check values (not keys) because role IDs are opaque UUIDs.

        Args:
            user:         Full user detail dict from GET /api/v1/User/{id}.
            role_keyword: Role name to look for, e.g. "Agent" or "Customer".

        Returns:
            True if role_keyword appears in rolesNames.values(), else False.
        """
        roles_names: dict = user.get("rolesNames", {})
        return role_keyword in roles_names.values()

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
        """
        Fetch all EspoCRM users whose rolesNames contains "Agent".

        Step 1: GET /api/v1/User          — paginated list of all user IDs
        Step 2: GET /api/v1/User/{id}     — full detail per user (needed for rolesNames)
        Step 3: Filter — keep only users where rolesNames values contain "Agent"

        Returns:
            List of full User dicts for all agents in the instance.
        """
        all_users = await self._get_all_users_with_detail()
        agents = [u for u in all_users if self._has_role(u, AGENT_ROLE_KEYWORD)]
        logger.info(
            "EspoCRM: found %d agent(s) out of %d total users",
            len(agents), len(all_users),
        )
        return agents

    async def get_agents_by_account(self, crm_org_id: str) -> list[dict]:
        """
        Fetch agents scoped to a specific EspoCRM Account.

        WHY THIS IS NON-TRIVIAL
        -----------------------
        EspoCRM User records carry no accountId FK — users are instance-level.
        The account relationship lives on the Contact entity: each Contact has
        an accountId pointing to its Account.

        CRITICAL: Contact.id != User.id — they are completely different records.
        The only reliable bridge is: Contact.emailAddress == User.emailAddress.

        STRATEGY
        --------
        Step 1: GET /api/v1/User  (paginated)
                → collect all user IDs in the instance

        Step 2: GET /api/v1/User/{id}  for each user
                → fetch full detail; rolesNames is only available here

        Step 3: Filter by rolesNames
                → keep only users whose rolesNames values contain "Agent"

        Step 4: GET /api/v1/Contact?accountId=<crm_org_id>  (paginated)
                → fetch all Contacts belonging to this Account

        Step 5: Cross-match on emailAddress
                → keep only agents whose emailAddress appears in the
                  account's contact list

        Example: TCS account has contact Rajnandini (rajnandini@tcs.com).
        After Steps 1-3 we have all instance-wide agents. Step 4 gives us
        TCS contacts. Step 5 keeps only Rajnandini — not every agent in
        the EspoCRM instance.

        Args:
            crm_org_id: EspoCRM Account UUID string
                        (crm_org_id from tenant_source_systems).

        Returns:
            List of User dicts for agents belonging to this account.
        """
        # Steps 1-3: fetch all users instance-wide, filter to agents only
        all_users  = await self._get_all_users_with_detail()
        all_agents = [u for u in all_users if self._has_role(u, AGENT_ROLE_KEYWORD)]

        logger.info(
            "EspoCRM: %d agent(s) found instance-wide, now scoping to account_id=%s",
            len(all_agents), crm_org_id,
        )

        if not all_agents:
            logger.info("EspoCRM: no agents in instance — nothing to scope")
            return []

        # Step 4: fetch all Contacts for this account
        contacts = await self.get_contacts_by_account(crm_org_id)

        if not contacts:
            logger.info(
                "EspoCRM: no contacts found for account_id=%s — no agents to return",
                crm_org_id,
            )
            return []

        # Step 5: cross-match agent emails against contact emails (case-insensitive)
        contact_emails: set[str] = {
            c["emailAddress"].lower()
            for c in contacts
            if c.get("emailAddress")
        }

        if not contact_emails:
            logger.warning(
                "EspoCRM: contacts exist for account_id=%s but none have emailAddress — "
                "cannot scope agents",
                crm_org_id,
            )
            return []

        scoped_agents = [
            agent for agent in all_agents
            if (agent.get("emailAddress") or "").lower() in contact_emails
        ]

        logger.info(
            "EspoCRM: %d agent(s) scoped to account_id=%s "
            "(matched against %d contact email(s))",
            len(scoped_agents), crm_org_id, len(contact_emails),
        )
        return scoped_agents

    # ------------------------------------------------------------------
    # Customer (User) endpoints
    # ------------------------------------------------------------------

    async def get_all_customers(self) -> list[dict]:
        """
        Fetch all EspoCRM users whose rolesNames contains "Customer".

        Step 1: GET /api/v1/User          — paginated list of all user IDs
        Step 2: GET /api/v1/User/{id}     — full detail per user (needed for rolesNames)
        Step 3: Filter — keep only users where rolesNames values contain "Customer"

        Note: No account scoping — customers are returned instance-wide.
              The Contact entity is not consulted here.

        Returns:
            List of full User dicts for all customers in the instance.
        """
        all_users = await self._get_all_users_with_detail()
        customers = [u for u in all_users if self._has_role(u, CUSTOMER_ROLE_KEYWORD)]
        logger.info(
            "EspoCRM: found %d customer(s) out of %d total users",
            len(customers), len(all_users),
        )
        return customers

    # ------------------------------------------------------------------
    # Contact endpoints
    # ------------------------------------------------------------------

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

        Used internally by get_agents_by_account() to scope agents:
          agent.emailAddress must appear in a Contact for this account.

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
            "EspoCRM: fetched %d contact(s) for account_id=%s",
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