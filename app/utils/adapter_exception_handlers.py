# app/utils/adapter_exception_handlers.py
"""
Adapter Layer Exception Handlers
=================================
Maps every exception from the adapter/credential/factory stack to a
correct HTTP response.  Registered on the FastAPI app at startup via
`register_adapter_exception_handlers(app)`.

Call this from your existing `register_exception_handlers()` in
app/utils/exceptions.py — just add one line at the bottom of that function:

    from app.utils.adapter_exception_handlers import register_adapter_exception_handlers
    register_adapter_exception_handlers(app)

HTTP mapping rationale
----------------------
CredentialNotFoundError   → 404  The integration hasn't been provisioned yet.
CredentialDecodeError     → 500  Secret is stored but corrupt — ops issue.
CredentialSaveError       → 500  Infisical write failed — ops issue.
CredentialDeleteError     → 500  Infisical delete failed — ops issue.
InfisicalConfigError      → 503  Bad env config — shouldn't reach prod requests.
AdapterFactoryError       → 500  Class path wrong or instantiation failed.
CapabilityNotSupportedError→ 400 Caller asked for something this CRM can't do.
CrmAuthError              → 502  CRM rejected our credentials — tenant issue.
CrmNotFoundError          → 404  Resource not found in the CRM.
CrmRateLimitError         → 429  Forward the upstream rate limit to the caller.
CrmServerError            → 502  Upstream CRM is having issues.
AuthenticationError       → 502  Adapter's authenticate() step failed.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.adapters.base.adapter import AdapterError, AuthenticationError
from app.adapters.base.client import (
    CrmAuthError,
    CrmClientError,
    CrmNotFoundError,
    CrmRateLimitError,
    CrmServerError,
)
from app.config.registry import AdapterNotFoundError, CapabilityNotSupportedError
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialDeleteError,
    CredentialError,
    CredentialNotFoundError,
    CredentialSaveError,
    InfisicalConfigError,
)
from app.factory.adapter_factory import AdapterFactoryError

logger = logging.getLogger(__name__)


def _json(status_code: int, detail: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "error_type": error_type},
    )


def register_adapter_exception_handlers(app: FastAPI) -> None:
    """Register all adapter-layer exception handlers on *app*."""

    # ── Credential layer ──────────────────────────────────────────────────

    @app.exception_handler(CredentialNotFoundError)
    async def handle_cred_not_found(
        request: Request, exc: CredentialNotFoundError
    ) -> JSONResponse:
        logger.warning(
            "Credential not found for integration_id='%s'", exc.integration_id
        )
        return _json(
            404,
            f"Integration '{exc.integration_id}' has not been provisioned. "
            "Use the /integrations endpoint to add credentials.",
            "credential_not_found",
        )

    @app.exception_handler(CredentialDecodeError)
    async def handle_cred_decode(
        request: Request, exc: CredentialDecodeError
    ) -> JSONResponse:
        logger.error(
            "Credential decode error for integration_id='%s': %s",
            exc.integration_id,
            exc,
        )
        return _json(
            500,
            f"Credentials for integration '{exc.integration_id}' are corrupted. "
            "Re-provision via the /integrations endpoint.",
            "credential_decode_error",
        )

    @app.exception_handler(CredentialSaveError)
    async def handle_cred_save(
        request: Request, exc: CredentialSaveError
    ) -> JSONResponse:
        logger.error("Credential save error: %s", exc)
        return _json(500, str(exc), "credential_save_error")

    @app.exception_handler(CredentialDeleteError)
    async def handle_cred_delete(
        request: Request, exc: CredentialDeleteError
    ) -> JSONResponse:
        logger.error("Credential delete error: %s", exc)
        return _json(500, str(exc), "credential_delete_error")

    @app.exception_handler(InfisicalConfigError)
    async def handle_infisical_config(
        request: Request, exc: InfisicalConfigError
    ) -> JSONResponse:
        logger.critical("Infisical misconfiguration: %s", exc)
        return _json(503, "Credential vault is misconfigured.", "infisical_config_error")

    # ── Registry / factory layer ──────────────────────────────────────────

    @app.exception_handler(AdapterNotFoundError)
    async def handle_adapter_not_found(
        request: Request, exc: AdapterNotFoundError
    ) -> JSONResponse:
        logger.warning("Adapter not found: %s", exc)
        return _json(404, str(exc), "adapter_not_found")

    @app.exception_handler(CapabilityNotSupportedError)
    async def handle_capability(
        request: Request, exc: CapabilityNotSupportedError
    ) -> JSONResponse:
        logger.warning("Capability not supported: %s", exc)
        return _json(400, str(exc), "capability_not_supported")

    @app.exception_handler(AdapterFactoryError)
    async def handle_factory_error(
        request: Request, exc: AdapterFactoryError
    ) -> JSONResponse:
        logger.error("AdapterFactory error: %s", exc)
        return _json(500, str(exc), "adapter_factory_error")

    # ── HTTP client layer ─────────────────────────────────────────────────

    @app.exception_handler(CrmAuthError)
    async def handle_crm_auth(request: Request, exc: CrmAuthError) -> JSONResponse:
        logger.warning("CRM auth error (bad credentials): %s", exc)
        return _json(
            502,
            "The CRM rejected the stored credentials. "
            "Re-provision via the /integrations endpoint.",
            "crm_auth_error",
        )

    @app.exception_handler(CrmNotFoundError)
    async def handle_crm_not_found(
        request: Request, exc: CrmNotFoundError
    ) -> JSONResponse:
        return _json(404, str(exc), "crm_not_found")

    @app.exception_handler(CrmRateLimitError)
    async def handle_crm_rate_limit(
        request: Request, exc: CrmRateLimitError
    ) -> JSONResponse:
        logger.warning("CRM rate limit hit: %s", exc)
        return _json(
            429,
            "The upstream CRM is rate-limiting requests. Retry after a moment.",
            "crm_rate_limit",
        )

    @app.exception_handler(CrmServerError)
    async def handle_crm_server(
        request: Request, exc: CrmServerError
    ) -> JSONResponse:
        logger.error("CRM server error: %s", exc)
        return _json(
            502,
            "The upstream CRM returned a server error. Try again later.",
            "crm_server_error",
        )

    @app.exception_handler(CrmClientError)
    async def handle_crm_client(
        request: Request, exc: CrmClientError
    ) -> JSONResponse:
        logger.error("CRM client error: %s", exc)
        return _json(500, str(exc), "crm_client_error")

    # ── Adapter layer ─────────────────────────────────────────────────────

    @app.exception_handler(AuthenticationError)
    async def handle_auth_error(
        request: Request, exc: AuthenticationError
    ) -> JSONResponse:
        logger.error("Adapter authentication failed: %s", exc)
        return _json(
            502,
            "Could not authenticate with the CRM. Check stored credentials.",
            "adapter_auth_error",
        )

    @app.exception_handler(AdapterError)
    async def handle_adapter_error(
        request: Request, exc: AdapterError
    ) -> JSONResponse:
        logger.error("Adapter error: %s", exc)
        return _json(500, str(exc), "adapter_error")

    logger.info("Adapter exception handlers registered.")