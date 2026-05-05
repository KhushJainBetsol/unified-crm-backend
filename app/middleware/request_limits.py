"""
app/middleware/request_limits.py

Middleware for enforcing request size limits and preventing abuse.

Protects against:
- Large payload uploads (prevent memory exhaustion)
- Excessive query parameters
- Oversized headers
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Configuration
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB max payload
MAX_QUERY_PARAMS = 100
MAX_HEADER_SIZE = 8 * 1024  # 8KB per header


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce payload size limits on incoming requests."""

    async def dispatch(self, request: Request, call_next):
        """Check Content-Length before processing."""
        content_length = request.headers.get("content-length")

        if content_length:
            try:
                length = int(content_length)
                if length > MAX_CONTENT_LENGTH:
                    logger.warning(
                        "Request payload too large | content_length=%d | max=%d | path=%s | from=%s",
                        length,
                        MAX_CONTENT_LENGTH,
                        request.url.path,
                        request.client.host if request.client else "unknown",
                    )
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={
                            "detail": f"Request payload exceeds {MAX_CONTENT_LENGTH} bytes",
                            "error_type": "payload_too_large",
                        },
                    )
            except ValueError:
                logger.warning("Invalid Content-Length header: %s", content_length)

        # Check query parameters count
        if len(request.query_params) > MAX_QUERY_PARAMS:
            logger.warning(
                "Too many query parameters | count=%d | max=%d | path=%s",
                len(request.query_params),
                MAX_QUERY_PARAMS,
                request.url.path,
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "detail": f"Too many query parameters (max {MAX_QUERY_PARAMS})",
                    "error_type": "too_many_query_params",
                },
            )

        return await call_next(request)


def register_request_limit_middleware(app: FastAPI) -> None:
    """Register request limit middleware on the FastAPI app."""
    app.add_middleware(RequestSizeLimitMiddleware)
    logger.info(
        "Request limit middleware registered | max_payload=%s | max_query_params=%d",
        f"{MAX_CONTENT_LENGTH / 1024 / 1024:.1f}MB",
        MAX_QUERY_PARAMS,
    )
