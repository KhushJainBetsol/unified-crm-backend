"""
app/dependencies/adapter_factory.py

Builds a singleton CrmAdapterFactory wired to your Postgres crm_integrations
table instead of Infisical (since credentials live in CrmIntegration rows).

Usage in routes:
    from app.dependencies.adapter_factory import get_ticket_service
    service: TicketService = Depends(get_ticket_service)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base.adapter import BaseCrmAdapter
from app.adapters.base.client import BaseCrmClient
from app.config.models import AdapterConfig
from app.config.registry import AdapterRegistry
from app.dependencies import get_db
from app.services.ticket_service import TicketService


# ---------------------------------------------------------------------------
# Credential envelope — mirrors what InfisicalCredentialManager would return
# but reads from your crm_integrations table instead.
# ---------------------------------------------------------------------------

class DbCredentialEnvelope:
    """Wraps a CrmIntegration row into the shape the factory expects."""

    def __init__(self, integration) -> None:
        # integration is a CrmIntegration ORM object
        self.crm_type: str = integration.source_system.system_name
        self.base_url: str = integration.base_url or ""
        self.credentials: dict = {
            "api_key": integration.api_key,
            "webhook_secret": integration.webhook_secret,
            "webhook_secrets": integration.webhook_secrets,
        }


# ---------------------------------------------------------------------------
# Postgres-backed credential manager
# ---------------------------------------------------------------------------

class DbCredentialManager:
    """
    Drop-in replacement for InfisicalCredentialManager.
    Reads credentials from the crm_integrations table synchronously
    by pre-loading a dict at factory-create time.

    Since CrmAdapterFactory.create() is synchronous, we pass in a pre-fetched
    dict of {integration_id_str: CrmIntegration} at construction time.
    """

    def __init__(self, integrations_by_id: dict[str, object]) -> None:
        self._map = integrations_by_id

    def get_credentials(self, integration_id: str) -> DbCredentialEnvelope:
        integration = self._map.get(integration_id)
        if not integration:
            raise KeyError(
                f"No CrmIntegration found for id='{integration_id}'. "
                "Ensure the row exists and is_active=True."
            )
        return DbCredentialEnvelope(integration)


# ---------------------------------------------------------------------------
# Registry — singleton, pre-warmed once at startup
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_registry() -> AdapterRegistry:
    """
    Returns the singleton AdapterRegistry, pre-warmed.
    lru_cache ensures this runs only once per process.
    Call registry.initialise() once in lifespan — this just returns it.
    """
    registry = AdapterRegistry(
        config_base_dir=Path("app/config"),
        manifest_filename="crm_adapters.yaml",
    )
    registry.initialise()
    return registry


# ---------------------------------------------------------------------------
# FastAPI dependency: TicketService with factory injected
# ---------------------------------------------------------------------------

async def get_ticket_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TicketService:
    """
    FastAPI dependency that constructs TicketService with a fully wired
    CrmAdapterFactory backed by the current DB session.

    The factory is lightweight to construct — the registry is cached,
    and the credential manager wraps an empty dict that gets populated
    lazily when factory.create(integration_id) is called.
    """
    from app.factory.adapter_factory import CrmAdapterFactory

    # We pass an empty map here — credentials are fetched inside
    # _push_update_to_crm which has the db session to query CrmIntegration.
    # The factory.create() call in _push_update_to_crm supplies integration_id
    # AFTER the service has already queried CrmIntegration from the DB.
    # So we build the manager lazily per-call inside the service.
    registry = get_registry()

    # Thin factory shell — credential manager is wired per-call in the service
    # (see TicketService._push_update_to_crm which queries the DB first,
    # then calls factory.create with a single-item manager)
    factory = _PerCallFactory(registry)

    return TicketService(db=db, adapter_factory=factory)


class _PerCallFactory:
    """
    Thin wrapper that delays credential resolution until create() is called.
    The service passes us a pre-fetched CrmIntegration object at call time.
    """

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    def create(self, integration_id: str, integration_obj=None) -> BaseCrmAdapter:
        """
        integration_obj: the CrmIntegration ORM row, passed by the service.
        This avoids a second DB round-trip inside the factory.
        """
        import importlib
        from app.factory.adapter_factory import AdapterFactoryError

        if integration_obj is None:
            raise AdapterFactoryError(
                f"_PerCallFactory.create() requires integration_obj to be passed "
                f"alongside integration_id='{integration_id}'."
            )

        crm_type = integration_obj.source_system.system_name

        try:
            entry = self._registry.get_entry(crm_type)
            config = self._registry.get_adapter_config(crm_type)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Config missing for crm_type='{crm_type}': {exc}"
            ) from exc

        # Dynamic import of adapter class
        try:
            module_path, class_name = entry.adapter_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            adapter_cls = getattr(module, class_name)
        except Exception as exc:
            raise AdapterFactoryError(
                f"Failed to import {entry.adapter_class}: {exc}"
            ) from exc

        client_cls = getattr(adapter_cls, "client_class", BaseCrmClient)

        client = client_cls(
            base_url=integration_obj.base_url or "",
            config=config,
            credentials={
                "api_key": integration_obj.api_key,
                "webhook_secret": integration_obj.webhook_secret,
            },
        )

        return adapter_cls(
            client=client,
            config=config,
            integration_id=str(integration_obj.id),
        )