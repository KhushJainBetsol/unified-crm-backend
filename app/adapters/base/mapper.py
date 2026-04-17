# crm/adapters/base/mapper.py
"""
SchemaMapper
============
Translates raw CRM API JSON payloads into unified domain models using the
field_mappings declared in each adapter's YAML config.

Design decisions
----------------
- Pure transformation — no I/O, no HTTP.  Trivially unit-testable.
- Dot-notation path resolution with optional-field support (paths starting
  with "?" resolve to None on KeyError rather than raising).
- Status and priority values are normalised via the config look-up tables.
- The raw payload is always attached to the model for debugging / passthrough.
- Adapters call ``mapper.to_ticket(raw_dict)``, never build UnifiedTicket
  themselves — this keeps normalisation logic in one place.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.config.models import AdapterConfig, FieldMappingConfig
from app.domain.models import (
    TicketPriority,
    TicketStatus,
    UnifiedAgent,
    UnifiedOrganization,
    UnifiedTicket,
)

logger = logging.getLogger(__name__)


def _resolve(data: Any, path: str) -> Any:
    """
    Resolve a (possibly dotted) path against *data*.

    A leading "?" makes the field optional — a missing key returns None
    instead of raising.  Without "?" a missing key also returns None but
    logs a warning so misconfigured mappings surface in logs.
    """
    optional = path.startswith("?")
    clean_path = path.lstrip("?")
    parts = clean_path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict):
            if not optional:
                logger.debug(
                    "Field path '%s' could not be resolved: "
                    "expected dict at '%s', got %s.",
                    clean_path,
                    part,
                    type(current).__name__,
                )
            return None
        if part not in current:
            if not optional:
                logger.debug(
                    "Field path '%s' not found in payload (key '%s' missing).",
                    clean_path,
                    part,
                )
            return None
        current = current[part]
    return current


class SchemaMapper:
    """
    YAML-driven mapping engine.

    Parameters
    ----------
    config:
        The fully-validated AdapterConfig for this CRM adapter.
    crm_type:
        Short lowercase CRM identifier (e.g. ``"zammad"``).
    integration_id:
        Tenant reference injected into every produced domain model.
    """

    def __init__(
        self,
        config: AdapterConfig,
        crm_type: str,
        integration_id: str,
    ) -> None:
        self._config = config
        self._crm_type = crm_type
        self._integration_id = integration_id
        self._mappings: FieldMappingConfig = config.field_mappings

    # ------------------------------------------------------------------
    # Public conversion methods
    # ------------------------------------------------------------------

    def to_ticket(self, raw: Dict[str, Any]) -> UnifiedTicket:
        """Map a raw CRM ticket payload to a UnifiedTicket."""
        m = self._mappings.ticket
        raw_status = str(_resolve(raw, m.get("status", "?status")) or "")
        raw_priority = str(_resolve(raw, m.get("priority", "?priority")) or "")

        return UnifiedTicket(
            id=str(_resolve(raw, m.get("id", "id")) or ""),
            crm_type=self._crm_type,
            integration_id=self._integration_id,
            title=_resolve(raw, m.get("title", "?title")),
            description=_resolve(raw, m.get("description", "?description")),
            status=self._map_status(raw_status),
            priority=self._map_priority(raw_priority),
            channel=_resolve(raw, m.get("channel", "?channel")),
            tags=self._coerce_list(_resolve(raw, m.get("tags", "?tags"))),
            assignee_id=self._to_str(_resolve(raw, m.get("assignee_id", "?assignee_id"))),
            customer_id=self._to_str(_resolve(raw, m.get("customer_id", "?customer_id"))),
            organization_id=self._to_str(
                _resolve(raw, m.get("organization_id", "?organization_id"))
            ),
            group=_resolve(raw, m.get("group", "?group")),
            created_at=_resolve(raw, m.get("created_at", "?created_at")),
            updated_at=_resolve(raw, m.get("updated_at", "?updated_at")),
            raw=raw,
        )

    def to_agent(self, raw: Dict[str, Any]) -> UnifiedAgent:
        """Map a raw CRM user/agent payload to a UnifiedAgent."""
        m = self._mappings.agent
        return UnifiedAgent(
            id=str(_resolve(raw, m.get("id", "id")) or ""),
            crm_type=self._crm_type,
            integration_id=self._integration_id,
            email=_resolve(raw, m.get("email", "?email")),
            first_name=_resolve(raw, m.get("first_name", "?first_name")),
            last_name=_resolve(raw, m.get("last_name", "?last_name")),
            active=bool(_resolve(raw, m.get("active", "?active")) or True),
            role=_resolve(raw, m.get("role", "?role")),
            created_at=_resolve(raw, m.get("created_at", "?created_at")),
            updated_at=_resolve(raw, m.get("updated_at", "?updated_at")),
            raw=raw,
        )

    def to_organization(self, raw: Dict[str, Any]) -> UnifiedOrganization:
        """Map a raw CRM organization payload to a UnifiedOrganization."""
        m = self._mappings.organization
        return UnifiedOrganization(
            id=str(_resolve(raw, m.get("id", "id")) or ""),
            crm_type=self._crm_type,
            integration_id=self._integration_id,
            name=_resolve(raw, m.get("name", "?name")),
            active=bool(_resolve(raw, m.get("active", "?active")) or True),
            created_at=_resolve(raw, m.get("created_at", "?created_at")),
            updated_at=_resolve(raw, m.get("updated_at", "?updated_at")),
            raw=raw,
        )

    def map_tickets(self, raw_list: List[Dict[str, Any]]) -> List[UnifiedTicket]:
        """Bulk-map a list of raw ticket dicts."""
        tickets = []
        for raw in raw_list:
            try:
                tickets.append(self.to_ticket(raw))
            except Exception:
                logger.exception(
                    "SchemaMapper failed to map ticket payload (id=%s). Skipping.",
                    raw.get("id"),
                )
        return tickets

    def map_agents(self, raw_list: List[Dict[str, Any]]) -> List[UnifiedAgent]:
        """Bulk-map a list of raw agent dicts."""
        agents = []
        for raw in raw_list:
            try:
                agents.append(self.to_agent(raw))
            except Exception:
                logger.exception(
                    "SchemaMapper failed to map agent payload (id=%s). Skipping.",
                    raw.get("id"),
                )
        return agents

    def map_organizations(
        self, raw_list: List[Dict[str, Any]]
    ) -> List[UnifiedOrganization]:
        """Bulk-map a list of raw organization dicts."""
        orgs = []
        for raw in raw_list:
            try:
                orgs.append(self.to_organization(raw))
            except Exception:
                logger.exception(
                    "SchemaMapper failed to map organization payload (id=%s). Skipping.",
                    raw.get("id"),
                )
        return orgs

    # ------------------------------------------------------------------
    # Private normalisation helpers
    # ------------------------------------------------------------------

    def _map_status(self, raw: str) -> TicketStatus:
        normalised = self._config.status_map.get(raw.lower(), "unknown")
        try:
            return TicketStatus(normalised)
        except ValueError:
            return TicketStatus.UNKNOWN

    def _map_priority(self, raw: str) -> TicketPriority:
        normalised = self._config.priority_map.get(raw.lower(), "unknown")
        try:
            return TicketPriority(normalised)
        except ValueError:
            return TicketPriority.UNKNOWN

    @staticmethod
    def _to_str(value: Any) -> Optional[str]:
        return str(value) if value is not None else None

    @staticmethod
    def _coerce_list(value: Any) -> list:
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]