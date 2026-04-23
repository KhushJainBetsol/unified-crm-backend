import logging
from typing import Any, Dict, Optional

from app.adapters.base.adapter import AuthenticationError, BaseCrmAdapter
from app.adapters.base.mapper import SchemaMapper
from app.domain.models import (
    PaginatedResult,
    UnifiedTicket,
    UnifiedAgent,
    UnifiedOrganization
)
from app.adapters.zammad.client import ZammadClient

logger = logging.getLogger(__name__)


class ZammadAdapter(BaseCrmAdapter):
    client_class = ZammadClient

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mapper = SchemaMapper(self._config, self.crm_type, self._integration_id)

    @property
    def crm_type(self) -> str:
        return "zammad"

    async def authenticate(self) -> None:
        """Verify credentials by hitting Zammad's /me endpoint."""
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
        self, page: int = 1, per_page: int = 100, filters: Optional[Dict[str, Any]] = None
    ) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("tickets")
        params = self._get_endpoint_params("tickets")
        params["page"] = page
        params["per_page"] = per_page
        if filters:
            params.update(filters)

        raw_response = await self._client.request("GET", path, params=params)
        raw_list = raw_response if isinstance(raw_response, list) else []
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

    async def fetch_agents(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("agents")
        params = self._get_endpoint_params("agents")
        raw_list = await self._client.paginate_all(path, extra_params=params)
        mapped_items = self._mapper.map_agents(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)
    
    async def fetch_customers(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("customers")
        params = self._get_endpoint_params("customers")
        raw_list = await self._client.paginate_all(path, extra_params=params)
        mapped_items = self._mapper.map_customers(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def fetch_organizations(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("organizations")
        raw_list = await self._client.paginate_all(path)
        mapped_items = self._mapper.map_organizations(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def verify_connection(self) -> Dict[str, Any]:
        """
        Verify stored credentials by hitting Zammad's token-validation endpoint.

        ``/api/v1/user_access_token`` returns the list of active API tokens for
        the authenticated user — a 200 confirms the token is valid and the
        user account is reachable.

        Returns
        -------
        Dict[str, Any]
            Wrapped response: ``{"tokens": [...], "count": N}``

        Raises
        ------
        AuthenticationError
            If Zammad returns a non-2xx or the request fails entirely.
        """
        self._assert_authenticated()
        path = self._get_endpoint("token-validation")  # /api/v1/user_access_token

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
        """Push status and priority updates back to Zammad."""
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