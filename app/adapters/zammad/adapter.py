import logging
from typing import Any, Dict, Optional

from app.adapters.base.adapter import AuthenticationError, BaseCrmAdapter
from app.adapters.base.mapper import SchemaMapper
from app.domain.models import (
    PaginatedResult,
    UnifiedTicket,
    UnifiedAgent,
    UnifiedComment,
    UnifiedOrganization
)
from app.adapters.zammad.client import ZammadClient
from app.integrations.normalizer.comment_normalizer import _extract_name, _extract_email, _parse_dt

logger = logging.getLogger(__name__)

AGENT_ROLE_ID    = 2
CUSTOMER_ROLE_ID = 3
SYSTEM_USER_ID   = 1


class ZammadAdapter(BaseCrmAdapter):
    client_class = ZammadClient

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mapper = SchemaMapper(self._config, self.crm_type, self._integration_id)

    @property
    def crm_type(self) -> str:
        return "zammad"

    async def authenticate(self) -> None:
        try:
            await self._client.request("GET", "/api/v1/users/me")
            self._authenticated = True
            logger.info(
                "[%s] Authenticated successfully for %s",
                self.crm_type,
                self._integration_id,
            )
        except Exception as exc:
            raise AuthenticationError(f"Zammad auth failed: {exc}") from exc

    async def fetch_tickets(
        self,
        crm_org_id: Optional[str] = None,
        page: int = 1,
        per_page: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> PaginatedResult:
        self._assert_authenticated()

        if crm_org_id:
            # Zammad uses a Lucene-style query string on the search endpoint.
            # e.g. GET /api/v1/tickets/search?query=organization_id:5&page=1&per_page=100
            path   = self._get_endpoint("tickets")
            params: Dict[str, Any] = {
                "query":    f"organization_id:{crm_org_id}",
                "page":     page,
                "per_page": per_page,
            }
            if filters:
                params.update(filters)
            logger.info(
                "[%s] fetch_tickets: scoping to organization_id=%s via search endpoint",
                self.crm_type,
                crm_org_id,
            )
        else:
            logger.warning(
                "[%s] fetch_tickets: crm_org_id not provided for integration_id=%s — "
                "returning all tickets without organization scoping",
                self.crm_type,
                self._integration_id,
            )
            path   = self._get_endpoint("tickets")
            params = self._get_endpoint_params("tickets")
            params["page"]     = page
            params["per_page"] = per_page
            if filters:
                params.update(filters)

        raw_response = await self._client.request("GET", path, params=params)
        raw_list     = raw_response if isinstance(raw_response, list) else []
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

    async def _fetch_users_for_org(self, crm_org_id: str) -> list[dict]:
        """
        Fetch users scoped to a specific org via search endpoint.
        Mirrors client.py's get_users_by_org() logic.
        """
        all_users: list[dict] = []
        page = 1

        while True:
            logger.info(
                "[%s] Fetching users for org_id=%s page=%d",
                self.crm_type, crm_org_id, page,
            )
            response = await self._client.request(
                "GET",
                "/api/v1/users/search",
                params={
                    "query":    f"organization_id:{crm_org_id}",
                    "per_page": 100,
                    "page":     page,
                },
            )
            batch: list[dict] = response if isinstance(response, list) else []
            if not batch:
                break
            all_users.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        logger.info(
            "[%s] _fetch_users_for_org: %d users fetched for org_id=%s",
            self.crm_type, len(all_users), crm_org_id,
        )
        return all_users

    async def fetch_agents(
        self,
        crm_org_id: str,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch agents (role_id=2), excluding system user (id=1).
        If crm_org_id is provided, scopes the search to that organization.
        """
        self._assert_authenticated()

        if crm_org_id:
            all_users = await self._fetch_users_for_org(crm_org_id)
        else:
            logger.warning(
                "[%s] fetch_agents: crm_org_id not provided for integration_id=%s — "
                "fetching all users without org scoping",
                self.crm_type,
                self._integration_id,
            )
            path = "/api/v1/users"
            all_users = await self._client.paginate_all(path)

        raw_agents = [
            u for u in all_users
            if u.get("id") != SYSTEM_USER_ID
            and AGENT_ROLE_ID in (u.get("role_ids") or [])
        ]

        logger.info(
            "[%s] fetch_agents: %d agents filtered from %d users (org_id=%s)",
            self.crm_type, len(raw_agents), len(all_users), crm_org_id,
        )

        mapped_items = self._mapper.map_agents(raw_agents)

        return PaginatedResult(
            items=mapped_items,
            page=1,
            per_page=per_page,
            has_more=False,
        )

    async def fetch_customers(
        self,
        crm_org_id: str,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch customers (role_id=3), excluding system user (id=1).
        If crm_org_id is provided, scopes the search to that organization.
        """
        self._assert_authenticated()

        if crm_org_id:
            all_users = await self._fetch_users_for_org(crm_org_id)
        else:
            logger.warning(
                "[%s] fetch_customers: crm_org_id not provided for integration_id=%s — "
                "fetching all users without org scoping",
                self.crm_type,
                self._integration_id,
            )
            path = "/api/v1/users"
            all_users = await self._client.paginate_all(path)

        raw_customers = [
            u for u in all_users
            if u.get("id") != SYSTEM_USER_ID
            and CUSTOMER_ROLE_ID in (u.get("role_ids") or [])
        ]

        logger.info(
            "[%s] fetch_customers: %d customers filtered from %d users (org_id=%s)",
            self.crm_type, len(raw_customers), len(all_users), crm_org_id,
        )

        mapped_items = self._mapper.map_customers(raw_customers)

        return PaginatedResult(
            items=mapped_items,
            page=1,
            per_page=per_page,
            has_more=False,
        )

    async def fetch_organizations(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path     = self._get_endpoint("organizations")
        raw_list = await self._client.paginate_all(path)
        mapped_items = self._mapper.map_organizations(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def verify_connection(self) -> Dict[str, Any]:
        self._assert_authenticated()
        path = self._get_endpoint("token-validation")

        try:
            result = await self._client.request("GET", path)
        except Exception as exc:
            raise AuthenticationError(
                f"[{self.crm_type}] Token validation failed for "
                f"integration_id={self._integration_id!r}: {exc}"
            ) from exc

        tokens: list = result if isinstance(result, list) else []
        logger.info(
            "[%s] Token validated — %d active token(s) found, integration_id=%s",
            self.crm_type,
            len(tokens),
            self._integration_id,
        )
        return {"tokens": tokens, "count": len(tokens)}

    async def push_ticket_update(self, crm_ticket_id: str, update_payload: Any) -> None:
        self._assert_authenticated()
        crm_data: dict = {}

        if update_payload.status is not None:
            mapped_status = next(
                (k for k, v in self._config.status_map.items()
                 if v == update_payload.status.lower()),
                None,
            )
            if mapped_status:
                crm_data["state"] = mapped_status
                if "pending" in mapped_status.lower() and update_payload.pending_until:
                    pt_str = update_payload.pending_until.isoformat()
                    if update_payload.pending_until.tzinfo is None:
                        pt_str += "Z"
                    crm_data["pending_time"] = pt_str
                else:
                    crm_data["pending_time"] = None

        if update_payload.priority is not None:
            mapped_priority = next(
                (k for k, v in self._config.priority_map.items()
                 if v == update_payload.priority.lower()),
                None,
            )
            if mapped_priority:
                crm_data["priority"] = mapped_priority

        if not crm_data:
            return

        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", crm_ticket_id)
        await self._client.request("PUT", path, json_body=crm_data)
        logger.info("[%s] Ticket %s updated: %s", self.crm_type, crm_ticket_id, crm_data)

    async def fetch_comments(self, crm_ticket_id: str) -> PaginatedResult:
        """
        Fetch all ticket articles from Zammad for a given ticket.

        The oldest article (by created_at) is marked is_first_article=True —
        CommentService uses this to update the ticket's description field
        rather than storing it as a comment row.
        """
        self._assert_authenticated()

        path = self._get_endpoint("comments").replace("{crm_ticket_id}", crm_ticket_id)
        response = await self._client.request("GET", path)

        raw_list: list[dict] = response if isinstance(response, list) else []
        if not raw_list:
            return PaginatedResult(items=[], page=1, per_page=100, has_more=False)

        # Sort ascending so index 0 is always the oldest (description) article
        sorted_articles = sorted(raw_list, key=lambda a: a.get("created_at") or "")

        items: list[UnifiedComment] = []
        for i, raw in enumerate(sorted_articles):
            crm_id = raw.get("id")
            if crm_id is None:
                continue

            from_field = raw.get("from") or raw.get("created_by") or ""
            author_name  = _extract_name(from_field)
            author_email = _extract_email(from_field)

            items.append(UnifiedComment(
                id               = str(crm_id),
                body             = raw.get("body"),
                comment_type     = raw.get("type"),
                author_name      = author_name,
                author_email     = author_email,
                is_internal      = bool(raw.get("internal", False)),
                created_at       = _parse_dt(raw.get("created_at")),
                updated_at       = _parse_dt(raw.get("updated_at")),
                is_first_article = (i == 0),
            ))

        return PaginatedResult(items=items, page=1, per_page=len(items), has_more=False)