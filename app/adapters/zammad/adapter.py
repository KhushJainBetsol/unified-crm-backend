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
    # The factory looks for this to inject the correct client
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
            logger.info(f"[{self.crm_type}] Authenticated successfully for {self._integration_id}")
        except Exception as exc:
            raise AuthenticationError(f"Zammad auth failed: {exc}") from exc

    async def fetch_tickets(
        self, page: int = 1, per_page: int = 100, filters: Optional[Dict[str, Any]] = None
    ) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("tickets")
        
        # 1. Grab default params from config (crucial for expand=true)
        params = self._get_endpoint_params("tickets")
        
        # 2. Apply explicit pagination parameters
        params["page"] = page
        params["per_page"] = per_page
        
        if filters:
            params.update(filters)
            
        # 3. Request exactly ONE page instead of `paginate_all`
        raw_response = await self._client.request("GET", path, params=params)
        
        # Zammad returns a list directly at the root
        raw_list = raw_response if isinstance(raw_response, list) else []
        mapped_items = self._mapper.map_tickets(raw_list)
        
        return PaginatedResult(
            items=mapped_items,
            page=page,
            per_page=per_page,
            has_more=len(mapped_items) == per_page
        )

    async def fetch_ticket_by_id(self, ticket_id: str) -> UnifiedTicket:
        self._assert_authenticated()
        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", ticket_id)
        raw_ticket = await self._client.request("GET", path)
        return self._mapper.to_ticket(raw_ticket)

    async def fetch_agents(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("agents")
        params = self._get_endpoint_params("agents") # Brings in role="Agent"
        
        raw_list = await self._client.paginate_all(path, extra_params=params)
        mapped_items = self._mapper.map_agents(raw_list)
        
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def fetch_organizations(self, page: int = 1, per_page: int = 100) -> PaginatedResult:
        self._assert_authenticated()
        path = self._get_endpoint("organizations")
        
        raw_list = await self._client.paginate_all(path)
        mapped_items = self._mapper.map_organizations(raw_list)
        
        return PaginatedResult(items=mapped_items, page=1, per_page=per_page, has_more=False)

    async def push_ticket_update(self, crm_ticket_id: str, update_payload: Any) -> None:
        """Push status and priority updates back to Zammad, handling the pending_time quirk."""
        self._assert_authenticated()
        crm_data: dict = {}

        if update_payload.status is not None:
            # Reverse lookup from our canonical status to Zammad's specific string
            mapped_status = next(
                (k for k, v in self._config.status_map.items() if v == update_payload.status.lower()), 
                None
            )
            
            if mapped_status:
                crm_data["state"] = mapped_status
                
                # Zammad Pending Time Quirks
                if "pending" in mapped_status.lower() and update_payload.pending_until:
                    pt_str = update_payload.pending_until.isoformat()
                    if update_payload.pending_until.tzinfo is None:
                        pt_str += "Z" # Force UTC if naive
                    crm_data["pending_time"] = pt_str
                else:
                    crm_data["pending_time"] = None # Clear it if moving to Open/Closed

        if update_payload.priority is not None:
            mapped_priority = next(
                (k for k, v in self._config.priority_map.items() if v == update_payload.priority.lower()), 
                None
            )
            if mapped_priority:
                crm_data["priority"] = mapped_priority

        if not crm_data:
            return

        path = self._get_endpoint("ticket_by_id").replace("{ticket_id}", crm_ticket_id)
        await self._client.request("PUT", path, json_body=crm_data)
        logger.info(f"[{self.crm_type}] Ticket {crm_ticket_id} updated: {crm_data}")