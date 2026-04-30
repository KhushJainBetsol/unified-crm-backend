# crm/adapters/base/auth.py
"""
Auth Strategies
===============
Implements the Strategy pattern for CRM authentication.

Each strategy encapsulates one auth method — how credentials are
translated into HTTP headers. Adding a new auth type means adding
a new class here only; BaseCrmClient never needs to change.

Strategies
----------
- ApiTokenAuth  — token injected into a configurable header with optional prefix
- BasicAuth     — RFC 7617 Basic auth (base64 username:password)
- OAuth2Auth    — Bearer token in Authorization header

Usage (internal — called by BaseCrmClient)
------------------------------------------
strategy = get_auth_strategy(config)
headers = strategy.build_headers(credentials)
"""

from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

from app.config.models import AdapterConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseAuthStrategy(ABC):
    """
    Contract that all auth strategies must satisfy.

    ``build_headers`` is the only method callers care about — it takes
    the raw credentials dict and returns a header dict ready to be passed
    to httpx.AsyncClient.
    """

    @abstractmethod
    def build_headers(self, credentials: Dict[str, Any]) -> Dict[str, str]:
        """
        Translate *credentials* into an HTTP header dict.

        Parameters
        ----------
        credentials:
            Plaintext secrets fetched from the DB / Infisical at runtime.
            Shape depends on the concrete strategy.

        Returns
        -------
        Dict[str, str]
            Header dict e.g. ``{"Authorization": "Token abc123"}``.
        """


# ---------------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------------

class ApiTokenAuth(BaseAuthStrategy):
    """
    API-token strategy.

    Injects a single token value into a configurable header with an
    optional prefix string.

    YAML config shape
    -----------------
    auth:
      strategy: api_token
      token_header: Authorization   # or X-Api-Key, X-Auth-Token, etc.
      token_prefix: "Token "        # include trailing space if needed

    Credential dict shape
    ---------------------
    {"token": "<value>"}
    or
    {"api_key": "<value>"}          # falls back to api_key if token absent
    """

    def __init__(self, token_header: str, token_prefix: str) -> None:
        self._header = token_header
        self._prefix = token_prefix

    def build_headers(self, credentials: Dict[str, Any]) -> Dict[str, str]:
        token = credentials.get("token") or credentials.get("api_key", "")
        header_value = f"{self._prefix}{token}"
        logger.debug(
            "ApiTokenAuth: injecting header '%s' (prefix=%r).",
            self._header,
            self._prefix,
        )
        return {self._header: header_value}


class BasicAuth(BaseAuthStrategy):
    """
    HTTP Basic authentication (RFC 7617).

    Encodes ``username:password`` as base64 and sets the standard
    ``Authorization: Basic <encoded>`` header.

    YAML config shape
    -----------------
    auth:
      strategy: basic

    Credential dict shape
    ---------------------
    {"username": "alice", "password": "s3cr3t"}
    """

    def build_headers(self, credentials: Dict[str, Any]) -> Dict[str, str]:
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        raw = f"{username}:{password}"
        encoded = base64.b64encode(raw.encode()).decode()
        logger.debug("BasicAuth: injecting Basic auth header for user '%s'.", username)
        return {"Authorization": f"Basic {encoded}"}


class OAuth2Auth(BaseAuthStrategy):
    """
    OAuth2 Bearer token strategy.

    Sets ``Authorization: Bearer <access_token>``.  The access_token is
    expected to already be present in the credentials dict — token
    refresh / exchange is handled separately in the adapter's
    ``authenticate()`` method, which can call
    ``client.update_auth_header()`` after obtaining a fresh token.

    YAML config shape
    -----------------
    auth:
      strategy: oauth2

    Credential dict shape
    ---------------------
    {"access_token": "<value>"}
    """

    def build_headers(self, credentials: Dict[str, Any]) -> Dict[str, str]:
        token = credentials.get("access_token", "")
        logger.debug("OAuth2Auth: injecting Bearer token.")
        return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Factory function — resolves strategy from config
# ---------------------------------------------------------------------------

def get_auth_strategy(config: AdapterConfig) -> BaseAuthStrategy:
    """
    Resolve and return the correct ``BaseAuthStrategy`` for *config*.

    Called once at client construction time by ``BaseCrmClient.__init__``.

    Raises
    ------
    ValueError
        If the strategy declared in the YAML is not recognised.
        (Pydantic's ``AuthConfig`` validator should catch this first at
        startup — this is a second line of defence.)
    """
    strategy = config.auth.strategy

    if strategy == "api_token":
        return ApiTokenAuth(
            token_header=config.auth.token_header or "Authorization",
            token_prefix=config.auth.token_prefix or "",
        )

    if strategy == "basic":
        return BasicAuth()

    if strategy == "oauth2":
        return OAuth2Auth()

    raise ValueError(
        f"Unsupported auth strategy: '{strategy}'. "
        "Add a new BaseAuthStrategy subclass in adapters/base/auth.py."
    )