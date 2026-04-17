# crm/adapters/base/client.py
"""
BaseCrmClient
=============
An async HTTP wrapper that every concrete CRM client inherits from.

Responsibilities
----------------
- Inject auth headers at request time (populated from live credentials).
- Execute GET / POST / PATCH / DELETE with exponential back-off retry.
- Abstract over all three pagination strategies declared in config:
    * page   — ?page=N&per_page=P
    * offset — ?offset=N&limit=P
    * cursor — ?cursor=<token>
- Extract the items array from an arbitrary JSONPath inside the response.
- Surface clean, typed exceptions so adapters don't leak httpx details.

Dependencies: httpx (async), tenacity (retry), jsonpath-ng (item extraction).
"""

from __future__ import annotations

import asyncio
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

    Parameters
    ----------
    base_url:
        The CRM instance root URL (e.g. ``"https://support.acme.com"``).
    config:
        The validated AdapterConfig for this CRM type.
    credentials:
        Plaintext credential dict retrieved from Infisical at runtime.
        Shape depends on the auth strategy declared in config:
          api_token → {"token": "<value>"}
          basic     → {"username": "x", "password": "y"}
          oauth2    → {"access_token": "<value>"}
    """

    def __init__(
        self,
        base_url: str,
        config: AdapterConfig,
        credentials: Dict[str, Any],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._config = config
        self._credentials = credentials
        self._http: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Create and configure the underlying httpx.AsyncClient."""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._config.http.timeout_seconds,
            headers=self._build_auth_headers(),
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
          - offset → increments ``?offset`` until fewer items than page_size
          - cursor → follows ``next_cursor`` until absent

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
        offset = 0
        limit = pag.default_page_size
        while True:
            params = {**base_params, "offset": offset, "limit": limit}
            raw = await self.request("GET", path, params=params)
            items = self._extract_items(raw, pag.items_path)
            if not items:
                break
            yield items
            if len(items) < limit:
                break
            offset += limit

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
            # Extract next cursor — look in the envelope, not the items
            cursor = self._extract_next_cursor(raw)
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_auth_headers(self) -> Dict[str, str]:
        """Build the Authorization header from credentials + config."""
        auth_cfg = self._config.auth
        strategy = auth_cfg.strategy

        if strategy == "api_token":
            token = self._credentials.get("token", "")
            prefix = auth_cfg.token_prefix or ""
            return {auth_cfg.token_header or "Authorization": f"{prefix}{token}"}

        if strategy == "basic":
            import base64
            raw = f"{self._credentials['username']}:{self._credentials['password']}"
            encoded = base64.b64encode(raw.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        if strategy == "oauth2":
            token = self._credentials.get("access_token", "")
            return {"Authorization": f"Bearer {token}"}

        raise CrmClientError(f"Unsupported auth strategy: '{strategy}'")

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map HTTP error codes to typed exceptions."""
        code = response.status_code
        if code < 400:
            return
        url = str(response.url)
        if code == 401 or code == 403:
            raise CrmAuthError(f"Auth error {code} from {url}")
        if code == 404:
            raise CrmNotFoundError(f"Resource not found (404): {url}")
        if code == 429:
            raise CrmRateLimitError(f"Rate limit hit (429): {url}")
        if code >= 500:
            raise CrmServerError(f"Server error {code}: {url}")
        raise CrmClientError(f"Unexpected HTTP {code}: {url}")

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