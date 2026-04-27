# crm/adapters/base/adapter.py
"""
BaseCrmAdapter
==============
The strict Abstract Base Class that EVERY concrete CRM adapter must implement.

Change: __init__ now accepts an optional crm_org_id parameter.
  This is the CRM-native Account UUID for this tenant (e.g. EspoCRM Account ID).
  It is sourced from the credentials envelope at factory time and stored as
  self._crm_org_id so subclasses (e.g. EspoCrmAdapter.fetch_agents) can use it
  to scope requests to a specific account without any extra DB lookups.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.adapters.base.client import BaseCrmClient
from app.config.models import AdapterConfig
from app.domain.models import (
    PaginatedResult,
    UnifiedAgent,
    UnifiedOrganization,
    UnifiedTicket,
)

logger = logging.getLogger(__name__)


class AdapterError(Exception):
    """Base class for adapter-level errors (distinct from HTTP errors)."""


class AuthenticationError(AdapterError):
    """Raised when the authenticate() step fails."""


class BaseCrmAdapter(ABC):
    """
    Abstract base for all CRM adapters.

    Parameters
    ----------
    client:
        An open (or about-to-be-opened) BaseCrmClient instance.
    config:
        The validated AdapterConfig for this CRM type.
    integration_id:
        Tenant reference — carried through into every unified model so
        the caller always knows which integration produced the data.
    crm_org_id:
        Optional CRM-native Account/Organization UUID for this tenant.
        Used by adapters (e.g. EspoCRM) to scope agent fetching to a
        specific account via the Contact cross-match strategy.
        Sourced from the credentials envelope (TenantSourceSystem.crm_org_id)
        at factory time and injected here — no extra DB lookup needed.
        None means no account scoping (return all instance-wide results).
    """

    def __init__(
        self,
        client: BaseCrmClient,
        config: AdapterConfig,
        integration_id: str,
        crm_org_id: Optional[str] = None,
    ) -> None:
        self._client         = client
        self._config         = config
        self._integration_id = integration_id
        self._crm_org_id     = crm_org_id      # None → no account scoping
        self._authenticated  = False

    # ------------------------------------------------------------------
    # Lifecycle — called by the factory
    # ------------------------------------------------------------------

    @abstractmethod
    async def authenticate(self) -> None:
        """
        Verify or establish the session with the CRM.

        For API-token auth this is usually a lightweight ``/whoami`` call.
        For OAuth2 it may exchange a refresh token for an access token and
        update the client's auth headers.

        Must set ``self._authenticated = True`` on success.

        Raises
        ------
        AuthenticationError
            If the CRM rejects the credentials.
        """

    async def open(self) -> None:
        """Open the underlying HTTP client and authenticate."""
        await self._client.open()
        await self.authenticate()
        logger.info(
            "[%s] Adapter opened and authenticated "
            "(integration_id=%s crm_org_id=%s).",
            self.crm_type,
            self._integration_id,
            self._crm_org_id,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.close()
        logger.info(
            "[%s] Adapter closed (integration_id=%s).",
            self.crm_type,
            self._integration_id,
        )

    async def __aenter__(self) -> "BaseCrmAdapter":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Abstract capability methods
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_tickets(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        filters: Optional[Dict[str, Any]] = None,
    ) -> PaginatedResult:
        """Fetch a page of tickets. items → List[UnifiedTicket]."""

    @abstractmethod
    async def fetch_ticket_by_id(self, ticket_id: str) -> UnifiedTicket:
        """Fetch a single ticket by CRM-native ID."""

    @abstractmethod
    async def fetch_agents(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """Fetch agents scoped to self._crm_org_id. items → List[UnifiedAgent]."""

    @abstractmethod
    async def fetch_customers(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """Fetch customers. items → List[UnifiedCustomer]."""

    @abstractmethod
    async def fetch_organizations(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """Fetch organizations. items → List[UnifiedOrganization]."""

    @abstractmethod
    async def push_ticket_update(
        self,
        crm_ticket_id: str,
        update_payload: Any,
    ) -> None:
        """Push a status/priority update to the CRM."""

    @abstractmethod
    async def verify_connection(self) -> Dict[str, Any]:
        """Lightweight credential check. Raises AuthenticationError on failure."""

    # ------------------------------------------------------------------
    # Concrete helpers available to all subclasses
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def crm_type(self) -> str:
        """Short lowercase CRM identifier, e.g. ``"espocrm"``."""

    def _assert_authenticated(self) -> None:
        if not self._authenticated:
            raise AuthenticationError(
                f"[{self.crm_type}] Adapter has not been authenticated. "
                "Ensure the factory calls adapter.open() before use."
            )

    def _get_endpoint(self, name: str) -> str:
        ep = self._config.endpoints.get(name)
        if ep is None:
            raise AdapterError(
                f"[{self.crm_type}] Endpoint '{name}' is not declared in "
                f"config. Available: {list(self._config.endpoints.keys())}"
            )
        return ep.path

    def _get_endpoint_params(self, name: str) -> Dict[str, Any]:
        ep = self._config.endpoints.get(name)
        return dict(ep.default_params) if ep else {}

    def _normalise_status(self, raw_status: Optional[str]) -> str:
        if raw_status is None:
            return "unknown"
        return self._config.status_map.get(raw_status.lower(), "unknown")

    def _normalise_priority(self, raw_priority: Optional[str]) -> str:
        if raw_priority is None:
            return "unknown"
        return self._config.priority_map.get(raw_priority.lower(), "unknown")

    def _stamp(self, model_dict: Dict[str, Any]) -> Dict[str, Any]:
        model_dict["crm_type"]       = self.crm_type
        model_dict["integration_id"] = self._integration_id
        return model_dict

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"crm_type={self.crm_type!r} "
            f"integration_id={self._integration_id!r} "
            f"crm_org_id={self._crm_org_id!r} "
            f"authenticated={self._authenticated}>"
        )