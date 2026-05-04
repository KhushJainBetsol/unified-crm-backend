import logging
from typing import Any, Dict, Optional

from app.adapters.base.adapter import AuthenticationError, BaseCrmAdapter
from app.adapters.base.mapper import SchemaMapper
from app.domain.models import (
    PaginatedResult,
    UnifiedTicket,
    UnifiedAgent,
    UnifiedComment,
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
        page: int = 1,
        per_page: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> PaginatedResult:
        self._assert_authenticated()


       
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

    async def fetch_agents(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch agents (role_id=2), excluding system user (id=1).
        If crm_org_id is provided, scopes the search to that organization.
        """
        self._assert_authenticated()

        
        path =  self._get_endpoint("agents")
        all_users = await self._client.paginate_all(path)

        raw_agents = [
            u for u in all_users
            if u.get("id") != SYSTEM_USER_ID
            and AGENT_ROLE_ID in (u.get("role_ids") or [])
        ]

        logger.info(
            "[%s] fetch_agents: %d agents filtered from %d users",
            self.crm_type, len(raw_agents), len(all_users),
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
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch customers (role_id=3), excluding system user (id=1).
        If crm_org_id is provided, scopes the search to that organization.
        """
        self._assert_authenticated()

        path =  self._get_endpoint("customers")
        all_users = await self._client.paginate_all(path)

        raw_customers = [
            u for u in all_users
            if u.get("id") != SYSTEM_USER_ID
            and CUSTOMER_ROLE_ID in (u.get("role_ids") or [])
        ]

        logger.info(
            "[%s] fetch_customers: %d customers filtered from %d users",
            self.crm_type, len(raw_customers), len(all_users),
        )

        mapped_items = self._mapper.map_customers(raw_customers)

        return PaginatedResult(
            items=mapped_items,
            page=1,
            per_page=per_page,
            has_more=False,
        )

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
        """
        Push ticket update to Zammad.
        
        CRM Contract for Zammad:
        - When state contains "pending" (e.g., "Pending" in Zammad):
          → pending_time MUST be set to an ISO 8601 timestamp string
        - For any other state (e.g., "New", "Open", "Closed"):
          → pending_time MUST be cleared (set to null) to avoid blocking ticket lifecycle
        
        Service layer validation (_validate_pending_for_crm) enforces that
        pending_until is provided when transitioning to pending, so this adapter
        can trust that if status is pending, the timestamp exists.
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
                # Zammad API expects state values in lowercase as they appear in config
                # e.g., "pending reminder", "closed", not "Pending reminder" or "Closed"
                # Send mapped_status as-is (lowercase from config keys)
                crm_data["state"] = mapped_status
                logger.debug(
                    "[%s] Ticket %s: state mapped from unified '%s' to Zammad state '%s'",
                    self.crm_type,
                    crm_ticket_id,
                    update_payload.status,
                    mapped_status,
                )
                
                # Zammad-specific: Handle pending_time based on pending status
                if "pending" in mapped_status.lower():
                    # Transitioning to pending state — timestamp must be set
                    if update_payload.pending_until:
                        pt_str = update_payload.pending_until.isoformat()
                        if update_payload.pending_until.tzinfo is None:
                            pt_str += "Z"  # Assume UTC if naive
                        crm_data["pending_time"] = pt_str
                        logger.info(
                            "[%s] Ticket %s: pending_time set to %s",
                            self.crm_type,
                            crm_ticket_id,
                            pt_str,
                        )
                    else:
                        # This should not occur if service-layer validation works correctly
                        logger.warning(
                            "[%s] Ticket %s: transitioned to pending state but "
                            "pending_until was None — setting pending_time to None",
                            self.crm_type,
                            crm_ticket_id,
                        )
                        crm_data["pending_time"] = None
                else:
                    # Not a pending state — clear pending_time to unblock ticket
                    crm_data["pending_time"] = None
                    logger.debug(
                        "[%s] Ticket %s: cleared pending_time (transitioning away from pending)",
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
                # Zammad API expects priority values as-is from config
                # Send mapped_priority as-is (could be integer ID or name)
                crm_data["priority"] = mapped_priority
                logger.debug(
                    "[%s] Ticket %s: priority mapped from unified '%s' to Zammad priority '%s'",
                    self.crm_type,
                    crm_ticket_id,
                    update_payload.priority,
                    mapped_priority,
                )

        if not crm_data:
            logger.debug(
                "[%s] Ticket %s: no mapped fields to update (status=%s, priority=%s)",
                self.crm_type,
                crm_ticket_id,
                update_payload.status,
                update_payload.priority,
            )
            return

        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", crm_ticket_id)
        logger.info(
            "[%s] Ticket %s: sending update — payload=%s",
            self.crm_type,
            crm_ticket_id,
            crm_data,
        )
        try:
            await self._client.request("PUT", path, json_body=crm_data)
            logger.info("[%s] Ticket %s updated successfully: %s", self.crm_type, crm_ticket_id, crm_data)
        except Exception as exc:
            logger.error(
                "[%s] Ticket %s update failed with payload %s: %s",
                self.crm_type,
                crm_ticket_id,
                crm_data,
                exc,
            )
            raise

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

    async def push_comment(
        self,
        crm_ticket_id: str,
        body: str,
        author_name: str,
    ) -> dict:
        """
        Post a new comment (article) to a ticket in Zammad.

        Returns a dict with at least an 'id' key, or a synthesized local ID
        if the CRM doesn't return one.
        """
        self._assert_authenticated()

        return await self._client.post_comment(
            crm_ticket_id=crm_ticket_id,
            body=body,
            author_name=author_name,
        )