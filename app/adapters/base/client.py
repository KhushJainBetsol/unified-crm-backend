# crm/adapters/base/client.py
"""
BaseCrmClient — unchanged except:
  - request() now accepts optional extra_headers: Dict[str, str]
    These are merged per-request and NOT stored on the client.
    Used by ZammadAdapter to inject X-On-Behalf-Of dynamically.
  - post_comment() added as a concrete helper that adapters call.
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


def _resolve_path(data: Any, path: Optional[str]) -> Any:
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
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, CrmServerError):
        return True
    if isinstance(exc, CrmRateLimitError):
        return True
    return False


class BaseCrmClient:
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
        self._auth_strategy: BaseAuthStrategy = (
            auth_strategy if auth_strategy is not None
            else get_auth_strategy(config)
        )

    async def open(self) -> None:
        auth_headers = self._auth_strategy.build_headers(self._credentials)
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._config.http.timeout_seconds,
            headers=auth_headers,
        )
        logger.debug("BaseCrmClient opened for '%s'.", self._base_url)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            logger.debug("BaseCrmClient closed for '%s'.", self._base_url)

    async def __aenter__(self) -> "BaseCrmClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def update_auth_header(self, new_credentials: Dict[str, Any]) -> None:
        self._ensure_open()
        new_headers = self._auth_strategy.build_headers(new_credentials)
        self._http.headers.update(new_headers)  # type: ignore[union-attr]
        logger.debug("Auth headers updated via update_auth_header().")

    # ------------------------------------------------------------------
    # Public request interface — NOW accepts extra_headers
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,  # NEW
    ) -> Any:
        """
        Execute a single HTTP request with retry / back-off.

        extra_headers — merged into this request only; not stored on the client.
        Used by adapters that need per-request headers (e.g. Zammad's
        X-On-Behalf-Of) without polluting the shared session headers.
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
                    headers=extra_headers or {},  # NEW — merged per-request
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
    # post_comment — concrete helper called by all adapters
    # ------------------------------------------------------------------

    async def post_comment(
        self,
        crm_ticket_id: str,
        body: str,
        author_name: str,
        json_body: Dict[str, Any],
        path: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> dict:
        """
        POST a comment payload to the CRM.

        Adapters construct json_body and path themselves (CRM-specific);
        this method handles the actual HTTP call + auth headers.

        Parameters
        ----------
        crm_ticket_id:
            External ticket ID — used only for logging.
        body:
            Comment text — used only for logging.
        author_name:
            Author display name — used only for logging.
        json_body:
            The fully-formed CRM-specific request body.
        path:
            The fully-formed endpoint path (with IDs substituted).
        extra_headers:
            Per-request headers — e.g. {"X-On-Behalf-Of": "agent@co.com"}
            for Zammad. None for EspoCRM.
        """
        logger.info(
            "post_comment → ticket=%s author=%s path=%s",
            crm_ticket_id,
            author_name,
            path,
        )
        result = await self.request(
            "POST",
            path,
            json_body=json_body,
            extra_headers=extra_headers,
        )
        # Normalise: always return a dict with at least an "id" key
        if isinstance(result, dict):
            return result
        logger.warning(
            "post_comment: CRM returned non-dict response for ticket=%s: %r",
            crm_ticket_id,
            result,
        )
        return {"id": str(crm_ticket_id) + "_comment", "raw": result}

    # ------------------------------------------------------------------
    # Pagination helpers — UNCHANGED
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        *,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[List[Any]]:
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
            raise CrmClientError(f"Unknown pagination strategy: '{pag.strategy}'")

    async def paginate_all(
        self,
        path: str,
        *,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        items: List[Any] = []
        async for page in self.paginate(path, extra_params=extra_params):
            items.extend(page)
        return items

    async def _paginate_by_page(
        self, path: str, base_params: Dict[str, Any], pag: PaginationConfig,
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
                break
            page += 1

    async def _paginate_by_offset(
        self, path: str, base_params: Dict[str, Any], pag: PaginationConfig,
    ) -> AsyncIterator[List[Any]]:
        offset = 0
        page_size = pag.default_page_size
        while True:
            params = {
                **base_params,
                pag.page_param:     offset,
                pag.per_page_param: page_size,
            }
            raw = await self.request("GET", path, params=params)
            items = self._extract_items(raw, pag.items_path)
            if not items:
                break
            yield items
            if pag.total_count_path:
                total = _resolve_path(raw, pag.total_count_path)
                if total is not None and (offset + len(items)) >= int(total):
                    break
            if len(items) < page_size:
                break
            offset += page_size

    async def _paginate_by_cursor(
        self, path: str, base_params: Dict[str, Any], pag: PaginationConfig,
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

    def _raise_for_status(self, response: httpx.Response) -> None:
        code = response.status_code
        if code < 400:
            return
        url = str(response.url)
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
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type or response.text.lstrip().startswith(("{", "[")):
            try:
                return response.json()
            except json.JSONDecodeError:
                pass
        return response.text

    @staticmethod
    def _extract_items(raw: Any, items_path: Optional[str]) -> List[Any]:
        if items_path is None:
            return raw if isinstance(raw, list) else []
        result = _resolve_path(raw, items_path)
        return result if isinstance(result, list) else []

    @staticmethod
    def _extract_next_cursor(raw: Any) -> Optional[str]:
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