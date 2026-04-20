# app/adapter_dependencies/deps.py
"""
FastAPI Dependency Injection — Adapter Layer
============================================
These dependency functions are the ONLY sanctioned way for routes and services
to access the adapter factory, credential manager, and registry.

Pattern
-------
Routes declare what they need via Depends():

    @router.get("/tickets")
    async def list_tickets(
        factory: CrmAdapterFactory = Depends(get_adapter_factory),
    ):
        adapter = factory.create(integration_id)
        async with adapter:
            result = await adapter.fetch_tickets()

Why not import app.state directly?
    Importing `app` in a route module creates a circular dependency
    (routes are registered ON the app). Pulling from `request.app.state`
    via a Depends() function breaks that cycle cleanly.

Why not use lru_cache / module-level singletons?
    The singletons already live on app.state (initialised in lifespan).
    Reading them through Depends() keeps the dependency graph explicit and
    makes unit testing trivial — inject a mock factory via app.dependency_overrides.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.config.registry import AdapterRegistry
from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory


# ---------------------------------------------------------------------------
# Core singleton accessors
# ---------------------------------------------------------------------------

def get_adapter_registry(request: Request) -> AdapterRegistry:
    """
    Return the pre-warmed AdapterRegistry from app.state.

    Raises 503 if the registry was never initialised (boot failure).
    """
    registry: AdapterRegistry | None = getattr(request.app.state, "adapter_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM adapter registry is not initialised. The service is starting up.",
        )
    return registry


def get_credential_manager(request: Request) -> AsyncInfisicalCredentialManager:
    """
    Return the AsyncInfisicalCredentialManager from app.state.

    Raises 503 if not initialised.
    """
    manager: AsyncInfisicalCredentialManager | None = getattr(
        request.app.state, "credential_manager", None
    )
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential manager is not initialised. The service is starting up.",
        )
    return manager


def get_adapter_factory(request: Request) -> CrmAdapterFactory:
    """
    Return the CrmAdapterFactory from app.state.

    Raises 503 if not initialised.
    """
    factory: CrmAdapterFactory | None = getattr(request.app.state, "adapter_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM adapter factory is not initialised. The service is starting up.",
        )
    return factory


# ---------------------------------------------------------------------------
# Higher-level composed dependencies
# ---------------------------------------------------------------------------

async def get_adapter_for_integration(
    integration_id: str,
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
):
    """
    Build and yield a fully-authenticated adapter for *integration_id*.

    This is an async generator dependency — FastAPI opens the adapter before
    the route handler runs and closes it after, even if an exception occurs.

    Usage in a route
    ----------------
    from typing import Annotated
    from app.adapters.base.adapter import BaseCrmAdapter

    @router.get("/integrations/{integration_id}/tickets")
    async def list_tickets(
        adapter: Annotated[BaseCrmAdapter, Depends(get_adapter_for_integration)],
    ):
        result = await adapter.fetch_tickets()
        return result.items

    Note: integration_id is extracted from the path param automatically by
    FastAPI because the dependency signature declares it as a plain str param
    that matches the route path parameter name.
    """
    try:
        adapter = factory.create(integration_id)
    except AdapterFactoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Could not build adapter for integration '{integration_id}': {exc}",
        )

    async with adapter:
        yield adapter


async def get_verified_adapter(
    integration_id: str,
    required_capability: str,
    factory: CrmAdapterFactory = Depends(get_adapter_factory),
    registry: AdapterRegistry = Depends(get_adapter_registry),
):
    """
    Like get_adapter_for_integration but validates a required capability first.

    Usage in a route that needs a specific capability
    -------------------------------------------------
    from functools import partial
    from fastapi import Depends

    require_ticket_fetch = partial(get_verified_adapter, required_capability="fetch_tickets")

    @router.get("/integrations/{integration_id}/tickets")
    async def list_tickets(
        adapter = Depends(require_ticket_fetch),
    ):
        result = await adapter.fetch_tickets()
        return result.items
    """
    from app.config.registry import CapabilityNotSupportedError

    # Look up crm_type from the credential envelope to check the registry
    try:
        cred_mgr: AsyncInfisicalCredentialManager = getattr(
            factory._cred_manager, "__wrapped__", factory._cred_manager
        )
        envelope = await cred_mgr.get_credentials(integration_id)
        crm_type = envelope.crm_type
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Integration '{integration_id}' not found: {exc}",
        )

    try:
        registry.assert_capability(crm_type, required_capability)
    except CapabilityNotSupportedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    try:
        adapter = factory.create(
            integration_id,
            required_capabilities=[required_capability],
        )
    except AdapterFactoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to build adapter: {exc}",
        )

    async with adapter:
        yield adapter