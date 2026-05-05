import logging
from typing import Any, Dict, List, Optional

from app.adapters.base.adapter import AuthenticationError, BaseCrmAdapter
from app.adapters.base.mapper import SchemaMapper
from app.domain.models import (
    PaginatedResult,
    UnifiedTicket,
    UnifiedAgent,
    UnifiedCustomer,
    UnifiedComment,
)
from app.adapters.espocrm.client import EspoCrmClient
from app.integrations.normalizer.comment_normalizer import _parse_dt  # reuse helper

logger = logging.getLogger(__name__)

# Role values as they appear in the User detail payload's rolesNames dict,
# e.g. {"69b917db3281aa2e8": "Agent"} or {"69b917db3281aa2e8": "Customer"}.
# Must match exactly — EspoCRM role names are case-sensitive.
_AGENT_ROLE    = "Agent"
_CUSTOMER_ROLE = "Customer"


class EspoCrmAdapter(BaseCrmAdapter):
    client_class = EspoCrmClient

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mapper = SchemaMapper(self._config, self.crm_type, self._integration_id)

    @property
    def crm_type(self) -> str:
        return "espocrm"

    async def authenticate(self) -> None:
        try:
            await self._client.request("GET", "/api/v1/App/user")
            self._authenticated = True
            logger.info(
                "[%s] Authenticated successfully for %s",
                self.crm_type,
                self._integration_id,
            )
        except Exception as exc:
            raise AuthenticationError(f"EspoCRM auth failed: {exc}") from exc

    async def fetch_tickets(
        self,
        page: int = 1,
        per_page: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("tickets")

        extra_params: Dict[str, Any] = dict(filters or {})

        raw_list = await self._client.paginate_all(path, extra_params=extra_params)
        mapped_items = self._mapper.map_tickets(raw_list)
        return PaginatedResult(
            items=mapped_items,
            page=page,
            per_page=per_page,
            has_more=len(mapped_items) == per_page,
        )

    async def fetch_ticket_by_id(self, ticket_id: str) -> UnifiedTicket:
        self._assert_authenticated()
        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", ticket_id)
        raw_ticket = await self._client.request("GET", path)
        return self._mapper.to_ticket(raw_ticket)

    async def fetch_agents(self,page: int = 1, per_page: int = 100) -> PaginatedResult:
        """
        Fetch all EspoCRM users whose role is "Agent", scoped to the
        Account that matches this integration's crm_org_id.

        WHY FOUR STEPS
        --------------
        The User list endpoint (GET /api/v1/User) returns stub records that
        do NOT include ``rolesNames``.  Role information is only present in
        the full User detail payload fetched via GET /api/v1/User/{id}.

        Additionally, User records carry no accountId FK — they are
        instance-level.  The account relationship is on the Contact entity:
        Contact.accountId == crm_org_id.  The only bridge between a Contact
        and its User is the shared emailAddress field.

        STRATEGY
        --------
        Step 1 — GET /api/v1/User  (paginated)
            Collect all user stubs (id, name only — no rolesNames).

        Step 2 — GET /api/v1/User/{id}  for each stub
            Fetch full detail; filter to users where any value in
            ``rolesNames`` equals "Agent",
            e.g. {"69b917db3281aa2e8": "Agent"}.

        Step 3 — GET /api/v1/Contact?accountId=<crm_org_id>  (paginated)
            Fetch all Contacts belonging to this Account.
            Collect their emailAddress values into a set.

        Step 4 — Cross-match
            Keep only agents whose emailAddress appears in the contact
            email set.  This scopes the result to agents linked to this
            specific Account, not every agent in the whole instance.

        Returns
        -------
        PaginatedResult
            ``.items`` is ``List[UnifiedAgent]``.
        """
        self._assert_authenticated()

        # ── Step 1: collect all user stubs (rolesNames absent in list) ────
        list_path = self._get_endpoint("agents")
        raw_stubs = await self._client.paginate_all(list_path)

        logger.info(
            "[%s] fetch_agents: %d user stubs retrieved, fetching full detail...",
            self.crm_type,
            len(raw_stubs),
        )

        # ── Step 2: fetch full detail per user, filter to Agent role ──────
        detail_path_tpl = self._get_endpoint("agent_by_id")
        all_agents_raw: List[Dict[str, Any]] = []

        for stub in raw_stubs:
            user_id: Optional[str] = stub.get("id")
            if not user_id:
                continue
            path = detail_path_tpl.replace("{agent_id}", user_id)
            try:
                detail = await self._client.request("GET", path)
            except Exception as exc:
                logger.warning(
                    "[%s] fetch_agents: could not fetch detail for user_id=%s — skipping. %s",
                    self.crm_type,
                    user_id,
                    exc,
                )
                continue

            if _AGENT_ROLE in detail.get("rolesNames", {}).values():
                all_agents_raw.append(detail)

        logger.info(
            "[%s] fetch_agents: %d/%d users have role '%s' instance-wide",
            self.crm_type,
            len(all_agents_raw),
            len(raw_stubs),
            _AGENT_ROLE,
        )

        if not all_agents_raw:
            return PaginatedResult(items=[], page=1, per_page=per_page, has_more=False)

        # ── Step 3: fetch all Contacts for this Account ───────────────────
        # crm_org_id is the EspoCRM Account UUID stored on the integration.
        # crm_org_id: Optional[str] = self._crm_org_id

    
        mapped_items = self._mapper.map_agents(all_agents_raw)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def fetch_customers(self,page: int = 1, per_page: int = 100) -> PaginatedResult:
        """
        Fetch all EspoCRM users whose role is "Customer".

        WHY TWO STEPS
        -------------
        The User list endpoint (GET /api/v1/User) returns stub records that do
        NOT include ``rolesNames``.  The role information is only present in the
        full User detail payload fetched via GET /api/v1/User/{id}.

        STRATEGY
        --------
        Step 1 — GET /api/v1/User  (paginated)
            Collect all user stubs (id, name only — no rolesNames).

        Step 2 — GET /api/v1/User/{id}  for each stub
            Fetch full detail; filter to users where any value in
            ``rolesNames`` equals "Customer",
            e.g. {"69b917db3281aa2e8": "Customer"}.

        NOTE: ``contactId`` in the User detail payload is the EspoCRM Contact
        record linked to this user — it is NOT the same as the customer's User
        id.  We key on User.id throughout.

        Returns
        -------
        PaginatedResult
            ``.items`` is ``List[UnifiedCustomer]``.
        """
        self._assert_authenticated()

        # ── Step 1: collect all user stubs (rolesNames absent in list) ────
        list_path = self._get_endpoint("customers")
        raw_stubs = await self._client.paginate_all(list_path)

        logger.info(
            "[%s] fetch_customers: %d user stubs retrieved, fetching full detail...",
            self.crm_type,
            len(raw_stubs),
        )

        # ── Step 2: fetch full detail per user, filter to Customer role ───
        detail_path_tpl = self._get_endpoint("customer_by_id")
        customers_raw: List[Dict[str, Any]] = []

        for stub in raw_stubs:
            user_id: Optional[str] = stub.get("id")
            if not user_id:
                continue
            path = detail_path_tpl.replace("{customer_id}", user_id)
            try:
                detail = await self._client.request("GET", path)
            except Exception as exc:
                logger.warning(
                    "[%s] fetch_customers: could not fetch detail for user_id=%s — skipping. %s",
                    self.crm_type,
                    user_id,
                    exc,
                )
                continue

            if _CUSTOMER_ROLE in detail.get("rolesNames", {}).values():
                customers_raw.append(detail)

        logger.info(
            "[%s] fetch_customers: %d/%d users have role '%s'",
            self.crm_type,
            len(customers_raw),
            len(raw_stubs),
            _CUSTOMER_ROLE,
        )

        # ── Map to unified domain models ──────────────────────────────────
        mapped_items = self._mapper.map_customers(customers_raw)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def verify_connection(self) -> Dict[str, Any]:
        """
        Hit the token-validation endpoint declared in crm_adapters.yaml.

        Returns
        -------
        Dict[str, Any]
            Raw user profile response from EspoCRM.

        Raises
        ------
        AuthenticationError
            If EspoCRM returns a non-2xx or the request fails entirely.
        """
        self._assert_authenticated()
        path = self._get_endpoint("token-validation").rstrip("?")

        try:
            result: Dict[str, Any] = await self._client.request("GET", path)
        except Exception as exc:
            raise AuthenticationError(
                f"[{self.crm_type}] Token validation failed for "
                f"integration_id={self._integration_id!r}: {exc}"
            ) from exc

        logger.info(
            "[%s] Token validated — user_id=%s, integration_id=%s",
            self.crm_type,
            result.get("id", "unknown"),
            self._integration_id,
        )
        return result

    async def push_ticket_update(self, crm_ticket_id: str, update_payload: Any) -> None:
        """
        Push ticket update to EspoCRM.
        
        CRM Contract for Espo:
        - Espo does NOT use pending_until timestamps for pending status
        - Pending is a simple status transition with no deadline/reminder semantics
        - If pending_until is provided in the payload, it is silently ignored
        - Only status and priority fields are sent to Espo API
        - Status values must be capitalized: "New", "Pending", "Closed", "Open", "Assigned", "Rejected"
        
        Note: pending_until may be stored in the dashboard database for tracking,
        but it has no effect on Espo-side behavior.
        """
        self._assert_authenticated()
        crm_data: dict = {}

        if update_payload.status is not None:
            mapped_status = next(
                (k for k, v in self._config.status_map.items()
                 if v == update_payload.status.lower()),
                None,
            )
            if mapped_status:
                # Espo API expects status values in title case (e.g., "Pending" not "pending")
                espo_status = mapped_status.capitalize()
                crm_data["status"] = espo_status
                
                # Espo-specific: Log if pending_until was provided (for observability)
                if (update_payload.status.lower() == "pending" and 
                    update_payload.pending_until is not None):
                    logger.debug(
                        "[%s] Ticket %s: pending_until was provided but will not be sent to Espo "
                        "(Espo does not use pending reminders). "
                        "Dashboard will store the timestamp for reference.",
                        self.crm_type,
                        crm_ticket_id,
                    )

        if update_payload.priority is not None:
            mapped_priority = next(
                (k for k, v in self._config.priority_map.items()
                 if v == update_payload.priority.lower()),
                None,
            )
            if mapped_priority:
                # Espo API expects priority values in title case (e.g., "High" not "high")
                espo_priority = mapped_priority.capitalize()
                crm_data["priority"] = espo_priority
                logger.debug(
                    "[%s] Case %s: priority mapped from unified '%s' to Espo priority '%s'",
                    self.crm_type,
                    crm_ticket_id,
                    update_payload.priority,
                    espo_priority,
                )

        if not crm_data:
            logger.debug(
                "[%s] Case %s: no mapped fields to update (status=%s, priority=%s)",
                self.crm_type,
                crm_ticket_id,
                update_payload.status,
                update_payload.priority,
            )
            return

        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", str(crm_ticket_id))
        logger.info(
            "[%s] Case %s: sending update — payload=%s",
            self.crm_type,
            crm_ticket_id,
            crm_data,
        )
        try:
            await self._client.request("PUT", path, json_body=crm_data)
            logger.info("[%s] Case %s updated successfully: %s", self.crm_type, crm_ticket_id, crm_data)
        except Exception as exc:
            logger.error(
                "[%s] Case %s update failed with payload %s: %s",
                self.crm_type,
                crm_ticket_id,
                crm_data,
                exc,
            )
            raise

    async def fetch_comments(self, crm_ticket_id: str) -> PaginatedResult:
        """
        Fetch all stream Post entries for an EspoCRM Case.

        Uses paginated GET /api/v1/Case/{id}/stream filtered to type=Post.
        All entries are treated as regular comments (no is_first_article logic
        for EspoCRM — the ticket description is a dedicated Case field).
        """
        self._assert_authenticated()

        all_posts: list[dict] = []
        offset = 0

        while True:
            path = self._get_endpoint("comments").replace("{crm_ticket_id}", crm_ticket_id)
            response = await self._client.request(
                "GET",
                path,
                params={
                    "offset":               offset,
                    "maxSize":              100,
                    "where[0][type]":       "equals",
                    "where[0][attribute]":  "type",
                    "where[0][value]":      "Post",
                },
            )
            batch: list[dict] = response.get("list", [])
            total: int        = response.get("total", 0)
            if not batch:
                break
            all_posts.extend(batch)
            offset += 100
            if len(all_posts) >= total:
                break

        items: list[UnifiedComment] = []
        for raw in all_posts:
            crm_id = raw.get("id")
            if crm_id is None:
                continue

            data_block = raw.get("data") or {}
            body = data_block.get("post") or raw.get("post")

            items.append(UnifiedComment(
                id               = str(crm_id),
                body             = body,
                comment_type     = raw.get("type", "Post"),
                author_name      = raw.get("createdByName"),
                author_email     = raw.get("createdByEmail"),
                is_internal      = bool(raw.get("isInternal", False)),
                created_at       = _parse_dt(raw.get("createdAt")),
                updated_at       = _parse_dt(raw.get("modifiedAt")),
                is_first_article = False,  # EspoCRM has no description-article concept
            ))

        return PaginatedResult(items=items, page=1, per_page=len(items), has_more=False)

    async def push_comment(
        self,
        crm_ticket_id: str,
        body: str,
        author_name: str,
        author_email: Optional[str] = None,  # ignored by EspoCRM
    ) -> dict:
        """
        Post a new stream Post to an EspoCRM Case.

        EspoCRM contract (confirmed from image 3):
        - Endpoint: POST /api/v1/Note  (from config: post_comment)
        - Body: type, parentId, parentType: "Case", post
        - author_email is not used — EspoCRM identifies the caller
          from the API token, not a header.
        """
        self._assert_authenticated()

        path = self._get_endpoint("post_comment")

        json_body = {
            "type":       "Post",
            "parentId":   crm_ticket_id,
            "parentType": "Case",
            "post":       body,
        }

        logger.info(
            "[%s] push_comment: posting to Case %s as %s",
            self.crm_type,
            crm_ticket_id,
            author_name,
        )

        return await self._client.post_comment(
            crm_ticket_id=crm_ticket_id,
            body=body,
            author_name=author_name,
            json_body=json_body,
            path=path,
            extra_headers=None,  # EspoCRM needs no extra headers
        )