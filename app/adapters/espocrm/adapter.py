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
from app.adapters.espocrm.client import EspoCrmClient

logger = logging.getLogger(__name__)


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
        self, page: int = 1, per_page: int = 100, filters: Optional[Dict[str, Any]] = None
    ) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("tickets")
        raw_list = await self._client.paginate_all(path, extra_params=filters)
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
        raw_list = await self._client.paginate_all(path)
        mapped_items = self._mapper.map_agents(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def fetch_organizations(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("organizations")
        raw_list = await self._client.paginate_all(path)
        mapped_items = self._mapper.map_organizations(raw_list)
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def verify_connection(self) -> Dict[str, Any]:
        """
        Hit the token-validation endpoint declared in crm_adapters.yaml.

        EspoCRM returns the current user's profile on success, giving us
        both proof-of-auth and a useful log line (user id) for free.

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
        path = self._get_endpoint("token-validation").rstrip("?")  # /api/v1/App/user

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
        self._assert_authenticated()
        crm_data: dict = {}

        if update_payload.status is not None:
            mapped_status = next(
                (k for k, v in self._config.status_map.items()
                 if v == update_payload.status.lower()),
                None,
            )
            if mapped_status:
                crm_data["status"] = mapped_status

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

        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", str(crm_ticket_id))
        await self._client.request("PUT", path, json_body=crm_data)
        logger.info("[%s] Case %s updated: %s", self.crm_type, crm_ticket_id, crm_data)