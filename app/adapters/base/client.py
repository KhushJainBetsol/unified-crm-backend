# crm/adapters/base/client.py
"""
BaseCrmClient
=============
An async HTTP wrapper that every concrete CRM client inherits from.

Responsibilities
----------------
- Inject auth headers at request time via a pluggable BaseAuthStrategy.
- Execute GET / POST / PATCH / DELETE with exponential back-off retry.
- Abstract over all three pagination strategies declared in config:
    * page   — ?page=N&per_page=P
    * offset — ?offset=N&maxSize=P   (param name from config.per_page_param)
    * cursor — ?cursor=<token>
- Extract the items array from an arbitrary JSONPath inside the response.
- Surface clean, typed exceptions so adapters don't leak httpx details.

Dependencies: httpx (async), tenacity (retry), jsonpath-ng (item extraction).
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.adapters.base.auth import BaseAuthStrategy, get_auth_strategy
from app.config.models import AdapterConfig, PaginationConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CrmClientError(Exception):
    """Base class for all HTTP client errors."""


class CrmAuthError(CrmClientError):
    """Raised on 401 / 403 responses."""


class CrmNotFoundError(CrmClientError):
    """Raised on 404 responses."""


class CrmRateLimitError(CrmClientError):
    """Raised on 429 responses (after retries are exhausted)."""


class CrmServerError(CrmClientError):
    """Raised on 5xx responses (after retries are exhausted)."""


# ---------------------------------------------------------------------------
# Helper — JSONPath-lite dot-notation resolver
# ---------------------------------------------------------------------------

def _resolve_path(data: Any, path: Optional[str]) -> Any:
    """
    Resolve a dot-notation path against *data*.

    Examples
    --------
    >>> _resolve_path({"a": {"b": 1}}, "a.b")
    1
    >>> _resolve_path({"items": [1, 2]}, "items")
    [1, 2]
    >>> _resolve_path({}, None)        # None path → return data as-is
    {}
    """
    if path is None:
        return data
    parts = path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _should_retry(exc: BaseException) -> bool:
    """Tenacity predicate — retry only on transient network / server errors."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, CrmServerError):
        return True
    if isinstance(exc, CrmRateLimitError):
        return True
    return False


# ---------------------------------------------------------------------------
# BaseCrmClient
# ---------------------------------------------------------------------------

class BaseCrmClient:
    """
    Async HTTP client base that handles auth injection, retries, and
    pagination.  Concrete adapters call the high-level helpers
    ``paginate()`` and ``request()``; they never build httpx calls directly.

    Auth is handled via the Strategy pattern — a ``BaseAuthStrategy``
    instance is resolved once at construction time from the YAML config
    and called during ``open()``.  To add a new auth method, add a new
    ``BaseAuthStrategy`` subclass in ``adapters/base/auth.py`` and register
    it in ``get_auth_strategy()``.  This class never needs to change.

    Parameters
    ----------
    base_url:
        The CRM instance root URL (e.g. ``"https://support.acme.com"``).
    config:
        The validated AdapterConfig for this CRM type.
    credentials:
        Plaintext credential dict retrieved from the DB / Infisical at runtime.
        Shape depends on the auth strategy declared in config:
          api_token → {"token": "<value>"} or {"api_key": "<value>"}
          basic     → {"username": "x", "password": "y"}
          oauth2    → {"access_token": "<value>"}
    auth_strategy:
        Optional — inject a custom BaseAuthStrategy directly (useful in tests).
        If None (default), the strategy is resolved automatically from config.
    """

    def __init__(
        self,
        base_url: str,
        config: AdapterConfig,
        credentials: Dict[str, Any],
        auth_strategy: Optional[BaseAuthStrategy] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._config = config
        self._credentials = credentials
        self._http: Optional[httpx.AsyncClient] = None

        # Strategy pattern: resolved once, never changed (unless OAuth2 adapter
        # calls update_auth_header() to inject a freshly exchanged token).
        self._auth_strategy: BaseAuthStrategy = (
            auth_strategy if auth_strategy is not None
            else get_auth_strategy(config)
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """
        Create and configure the underlying httpx.AsyncClient.

        Auth headers are built by delegating to the injected strategy —
        no auth logic lives in this method.
        """
        auth_headers = self._auth_strategy.build_headers(self._credentials)
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._config.http.timeout_seconds,
            headers=auth_headers,
        )
        logger.debug("BaseCrmClient opened for '%s'.", self._base_url)

    async def close(self) -> None:
        """Cleanly close the httpx client and release connections."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            logger.debug("BaseCrmClient closed for '%s'.", self._base_url)

    async def __aenter__(self) -> "BaseCrmClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Auth utility — used by OAuth2 adapters after token refresh
    # ------------------------------------------------------------------

    def update_auth_header(self, new_credentials: Dict[str, Any]) -> None:
        """
        Re-build and apply auth headers from *new_credentials*.

        Intended for OAuth2 adapters that exchange a refresh token for a
        new access_token inside ``authenticate()``.  After calling this,
        all subsequent requests will carry the updated token.

        Example (in an OAuth2 adapter's authenticate method)
        -----------------------------------------------------
        token_response = await self._client.request("POST", "/oauth/token", ...)
        self._client.update_auth_header({"access_token": token_response["access_token"]})
        """
        self._ensure_open()
        new_headers = self._auth_strategy.build_headers(new_credentials)
        self._http.headers.update(new_headers)  # type: ignore[union-attr]
        logger.debug("Auth headers updated via update_auth_header().")

    # ------------------------------------------------------------------
    # Public request interface
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Execute a single HTTP request with retry / back-off.

        Returns the parsed JSON body.

        Raises
        ------
        CrmAuthError        on 401 / 403
        CrmNotFoundError    on 404
        CrmRateLimitError   on 429 (after all retries)
        CrmServerError      on 5xx  (after all retries)
        CrmClientError      on any other non-2xx status
        """
        self._ensure_open()

        cfg = self._config.http
        attempt_log: List[Tuple[int, str]] = []

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(cfg.max_retries),
            wait=wait_exponential(
                multiplier=cfg.backoff_base, min=cfg.backoff_base, max=60
            ),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                response = await self._http.request(  # type: ignore[union-attr]
                    method=method.upper(),
                    url=path,
                    params=params,
                    json=json_body,
                )
                attempt_log.append((response.status_code, path))
                self._raise_for_status(response)

        logger.debug(
            "HTTP %s %s → %s (attempts: %d)",
            method.upper(),
            path,
            attempt_log[-1][0],
            len(attempt_log),
        )
        return self._parse_response(response)  # type: ignore[possibly-undefined]

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        *,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[List[Any]]:
        """
        Async generator that yields one *page* of items at a time.

        Supports all three strategies declared in config.pagination.strategy:
          - page   → increments ``?page`` until an empty page is returned
          - offset → increments ``?offset`` using per_page_param from config
                     (e.g. "maxSize" for EspoCRM, "limit" for others)
          - cursor → follows ``next_cursor`` until absent

        extra_params are merged into every paginated request — this is how
        the EspoCRM adapter passes where[] filters for account scoping.

        Usage
        -----
        async for page in client.paginate("/api/v1/tickets"):
            for item in page:
                process(item)
        """
        pag = self._config.pagination
        params: Dict[str, Any] = dict(extra_params or {})
        params[pag.per_page_param] = pag.default_page_size

        if pag.strategy == "page":
            async for page in self._paginate_by_page(path, params, pag):
                yield page
        elif pag.strategy == "offset":
            async for page in self._paginate_by_offset(path, params, pag):
                yield page
        elif pag.strategy == "cursor":
            async for page in self._paginate_by_cursor(path, params, pag):
                yield page
        else:
            raise CrmClientError(
                f"Unknown pagination strategy: '{pag.strategy}'"
            )

    async def paginate_all(
        self,
        path: str,
        *,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """Convenience wrapper — exhausts all pages and returns a flat list."""
        items: List[Any] = []
        async for page in self.paginate(path, extra_params=extra_params):
            items.extend(page)
        return items

    # ------------------------------------------------------------------
    # Private pagination strategies
    # ------------------------------------------------------------------

    async def _paginate_by_page(
        self,
        path: str,
        base_params: Dict[str, Any],
        pag: PaginationConfig,
    ) -> AsyncIterator[List[Any]]:
        page = 1
        while True:
            params = {**base_params, pag.page_param: page}
            raw = await self.request("GET", path, params=params)
            items = self._extract_items(raw, pag.items_path)
            if not items:
                break
            yield items
            if len(items) < pag.default_page_size:
                break  # last page (partial)
            page += 1

    async def _paginate_by_offset(
        self,
        path: str,
        base_params: Dict[str, Any],
        pag: PaginationConfig,
    ) -> AsyncIterator[List[Any]]:
        """
        Offset-based pagination.

        FIX: previously hardcoded "limit" as the page-size param name.
        Now correctly uses ``pag.per_page_param`` (e.g. "maxSize" for EspoCRM)
        so the page-size param matches what each CRM actually expects.

        Also uses ``pag.total_count_path`` when available to stop early
        instead of waiting for an empty page, which saves one extra request.
        """
        offset = 0
        page_size = pag.default_page_size

        while True:
            params = {
                **base_params,
                pag.page_param:     offset,      # e.g. "offset" → 0, 100, 200 …
                pag.per_page_param: page_size,   # e.g. "maxSize" → 100  (was "limit" — BUG FIXED)
            }
            raw = await self.request("GET", path, params=params)
            items = self._extract_items(raw, pag.items_path)

            if not items:
                break

            yield items

            # Early-exit using total count when the response provides it
            # (e.g. EspoCRM returns {"list": [...], "total": 42})
            if pag.total_count_path:
                total = _resolve_path(raw, pag.total_count_path)
                if total is not None and (offset + len(items)) >= int(total):
                    break

            if len(items) < page_size:
                break  # partial page → last page

            offset += page_size

    async def _paginate_by_cursor(
        self,
        path: str,
        base_params: Dict[str, Any],
        pag: PaginationConfig,
    ) -> AsyncIterator[List[Any]]:
        cursor: Optional[str] = None
        while True:
            params = {**base_params}
            if cursor:
                params["cursor"] = cursor
            raw = await self.request("GET", path, params=params)
            items = self._extract_items(raw, pag.items_path)
            if not items:
                break
            yield items
            cursor = self._extract_next_cursor(raw)
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map HTTP error codes to typed exceptions."""
        code = response.status_code
        if code < 400:
            return
        url = str(response.url)
        
        # Try to extract error details from response body
        try:
            error_body = self._parse_response(response)
            error_detail = f"HTTP {code}: {url} — Response: {json.dumps(error_body)}"
        except Exception:
            error_detail = f"HTTP {code}: {url} — Body: {response.text[:500]}"
        
        if code in (401, 403):
            raise CrmAuthError(error_detail)
        if code == 404:
            raise CrmNotFoundError(error_detail)
        if code == 429:
            raise CrmRateLimitError(error_detail)
        if code >= 500:
            raise CrmServerError(error_detail)
        raise CrmClientError(error_detail)

    @staticmethod
    def _parse_response(response: httpx.Response) -> Any:
        """Return parsed JSON or raw text if the body is not JSON."""
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type or response.text.lstrip().startswith(("{", "[")):
            try:
                return response.json()
            except json.JSONDecodeError:
                pass
        return response.text

    @staticmethod
    def _extract_items(raw: Any, items_path: Optional[str]) -> List[Any]:
        """
        Pull the items array out of a response envelope.
        If items_path is None, the response itself is expected to be a list.
        """
        if items_path is None:
            return raw if isinstance(raw, list) else []
        result = _resolve_path(raw, items_path)
        return result if isinstance(result, list) else []

    @staticmethod
    def _extract_next_cursor(raw: Any) -> Optional[str]:
        """Look for a 'next_cursor' or 'meta.next_cursor' key in the envelope."""
        if not isinstance(raw, dict):
            return None
        return (
            raw.get("next_cursor")
            or _resolve_path(raw, "meta.next_cursor")
            or _resolve_path(raw, "pagination.next_cursor")
        )

    def _ensure_open(self) -> None:
        if self._http is None:
            raise CrmClientError(
                "BaseCrmClient is not open. Call `await client.open()` or use "
                "it as an async context manager (`async with client`)."
            )