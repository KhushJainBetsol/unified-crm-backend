# crm/adapters/base/adapter.py
"""
BaseCrmAdapter
==============
The strict Abstract Base Class that EVERY concrete CRM adapter must implement.

Architectural contract
----------------------
- Each method maps to a capability string declared in crm_adapters.yaml
  (e.g. `fetch_tickets` ↔ capability ``"fetch_tickets"``).
- All methods are async.
- All methods return either a unified domain model or a PaginatedResult
  wrapping unified domain models — never raw CRM JSON.
- The adapter holds a BaseCrmClient instance (injected by the factory).
  It does NOT construct the client itself; that is the factory's job.
- ``authenticate()`` is always called first by the factory after construction.
  Adapters may use it to verify credentials or fetch an OAuth token.

Why ABC and not Protocol?
  Protocol is ideal for structural subtyping (duck typing).  Here we want
  *nominal* subtyping: if you forget to implement ``fetch_agents`` the
  interpreter raises at class-definition time, not at call time.  ABC gives
  us that guarantee.
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
    """

    def __init__(
        self,
        client: BaseCrmClient,
        config: AdapterConfig,
        integration_id: str,
    ) -> None:
        self._client = client
        self._config = config
        self._integration_id = integration_id
        self._authenticated = False

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
        """
        Open the underlying HTTP client and authenticate.
        Called by the factory — adapters should not call this themselves.
        """
        await self._client.open()
        await self.authenticate()
        logger.info(
            "[%s] Adapter opened and authenticated (integration_id=%s).",
            self.crm_type,
            self._integration_id,
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
        """
        Fetch a page of tickets.

        Parameters
        ----------
        page:
            1-based page number (ignored for cursor-based pagination).
        per_page:
            Maximum items to return per page.
        filters:
            Optional CRM-agnostic filter dict (e.g. ``{"status": "open"}``).
            Adapters translate these into CRM-specific query params.

        Returns
        -------
        PaginatedResult
            ``.items`` is a ``List[UnifiedTicket]``.
        """

    @abstractmethod
    async def fetch_ticket_by_id(self, ticket_id: str) -> UnifiedTicket:
        """
        Fetch a single ticket by its CRM-native ID.

        Raises
        ------
        CrmNotFoundError
            If the ticket does not exist.
        """

    @abstractmethod
    async def fetch_agents(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch a page of agents / users.

        Returns
        -------
        PaginatedResult
            ``.items`` is a ``List[UnifiedAgent]``.
        """

    @abstractmethod
    async def fetch_customers(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch a page of customers / users.

        Returns
        -------
        PaginatedResult
            ``.items`` is a ``List[UnifiedAgent]``.
        """

    @abstractmethod
    async def fetch_organizations(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> PaginatedResult:
        """
        Fetch a page of organizations.

        Returns
        -------
        PaginatedResult
            ``.items`` is a ``List[UnifiedOrganization]``.
        """

    @abstractmethod
    async def push_ticket_update(
        self,
        crm_ticket_id: str,
        update_payload: Any,
    ) -> None:
        """
        Push an update to the CRM.
        The adapter is responsible for translating the unified domain payload
        (e.g., status="open") into the CRM's native JSON format using its config.
        """

    @abstractmethod
    async def verify_connection(self) -> Dict[str, Any]:
        """
        Perform a lightweight, read-only call to the CRM to confirm the
        stored credentials are accepted.

        Called by the integrations router immediately after provisioning or
        rotating credentials — before the DB record is committed — so a bad
        token is caught early and the caller gets a clear 502 rather than a
        silent write of unusable creds.

        Returns
        -------
        Dict[str, Any]
            Raw response from the CRM's token-validation (or whoami) endpoint.
            The router discards this; it is returned so adapters can log it.

        Raises
        ------
        AuthenticationError
            If the CRM returns a non-2xx response or rejects the token.
        """

    # ------------------------------------------------------------------
    # Concrete helpers available to all subclasses
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def crm_type(self) -> str:
        """
        A short lowercase identifier for this CRM type, e.g. ``"zammad"``.
        Used in domain models and log output.
        Must be implemented as a class-level property in each adapter.
        """

    def _assert_authenticated(self) -> None:
        """Guard that concrete methods can call at the top of each method."""
        if not self._authenticated:
            raise AuthenticationError(
                f"[{self.crm_type}] Adapter has not been authenticated. "
                "Ensure the factory calls adapter.open() before use."
            )

    def _get_endpoint(self, name: str) -> str:
        """
        Retrieve an endpoint path from config by name.

        Raises
        ------
        AdapterError
            If *name* is not declared in the config's endpoints section.
        """
        ep = self._config.endpoints.get(name)
        if ep is None:
            raise AdapterError(
                f"[{self.crm_type}] Endpoint '{name}' is not declared in "
                f"config.  Available: {list(self._config.endpoints.keys())}"
            )
        return ep.path

    def _get_endpoint_params(self, name: str) -> Dict[str, Any]:
        """Return the default_params dict for an endpoint, or empty dict."""
        ep = self._config.endpoints.get(name)
        return dict(ep.default_params) if ep else {}

    def _normalise_status(self, raw_status: Optional[str]) -> str:
        """Map a raw CRM status string to our canonical value via config."""
        if raw_status is None:
            return "unknown"
        return self._config.status_map.get(raw_status.lower(), "unknown")

    def _normalise_priority(self, raw_priority: Optional[str]) -> str:
        """Map a raw CRM priority string to our canonical value via config."""
        if raw_priority is None:
            return "unknown"
        return self._config.priority_map.get(raw_priority.lower(), "unknown")

    def _stamp(self, model_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Inject crm_type and integration_id into a model dict."""
        model_dict["crm_type"] = self.crm_type
        model_dict["integration_id"] = self._integration_id
        return model_dict

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"crm_type={self.crm_type!r} "
            f"integration_id={self._integration_id!r} "
            f"authenticated={self._authenticated}>"
        )