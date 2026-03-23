"""
app/utils/exceptions.py

Custom application exceptions and global FastAPI exception handlers.

Centralising exception handling means:
  - Every error returns the same response shape
  - No try/except boilerplate scattered across routes
  - Errors are logged consistently in one place
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom application exceptions
# ---------------------------------------------------------------------------

class AppError(Exception):
    """Base class for all application errors."""
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.message
        super().__init__(self.message)


class NotFoundError(AppError):
    """Resource not found."""
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found"


class ValidationError(AppError):
    """Business rule validation failed."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Validation failed"


class SyncError(AppError):
    """CRM sync operation failed."""
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "CRM sync failed"


class CRMConnectionError(AppError):
    """Cannot connect to external CRM."""
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "Cannot connect to CRM — check credentials and URL in .env"


# ---------------------------------------------------------------------------
# Global exception handlers — registered in main.py
# ---------------------------------------------------------------------------

def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "message": message, "data": None},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all global exception handlers on the FastAPI app.
    Call this once in main.py after creating the app instance.
    """

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "AppError on %s %s: %s",
            request.method, request.url.path, exc.message,
        )
        return _error_response(exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Extract the first validation error message for a clean response
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = " → ".join(str(loc) for loc in first.get("loc", []))
        msg = first.get("msg", "Validation error")
        message = f"{field}: {msg}" if field else msg

        logger.warning(
            "Validation error on %s %s: %s",
            request.method, request.url.path, message,
        )
        return _error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, message)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        logger.error(
            "DB integrity error on %s %s: %s",
            request.method, request.url.path, str(exc.orig),
        )
        return _error_response(
            status.HTTP_409_CONFLICT,
            "A record with this data already exists",
        )

    @app.exception_handler(OperationalError)
    async def operational_error_handler(
        request: Request, exc: OperationalError
    ) -> JSONResponse:
        logger.error(
            "DB operational error on %s %s: %s",
            request.method, request.url.path, str(exc),
        )
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Database is unavailable — please try again shortly",
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "Unhandled exception on %s %s",
            request.method, request.url.path,
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "An unexpected error occurred",
        )