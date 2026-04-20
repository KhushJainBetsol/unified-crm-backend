# app/services/crm_sync_service.py
"""
CrmSyncService
==============
Replaces the old monolithic sync logic that contained:

    if integration.crm_type == "zammad":
        client = ZammadClient(...)
        raw = client.get_tickets()
        tickets = [map_zammad_ticket(t) for t in raw]
    elif integration.crm_type == "espocrm":
        ...

New pattern: the service is 100% CRM-agnostic.
It only knows BaseCrmAdapter — the factory handles everything else.

Migration note (Phase 4 step 11)
---------------------------------
This service runs behind the CRM_ADAPTER_ENGINE feature flag.
Set CRM_ADAPTER_ENGINE=legacy to keep the old path running in parallel.
Set CRM_ADAPTER_ENGINE=new to activate this service.
Flip at runtime — no redeployment required.

Your scheduler calls `run_all_tenants_full_sync()` in scheduler.py.
Point that function at `CrmSyncService.sync_all_integrations()` once
the flag is permanently on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base.adapter import BaseCrmAdapter
from app.adapters.base.client import CrmClientError
from app.core.database import async_session_maker
from app.core.settings import get_settings
from app.domain.models import UnifiedAgent, UnifiedOrganization, UnifiedTicket
from app.factory.adapter_factory import AdapterFactoryError, CrmAdapterFactory
from app.models.integration import Integration  # your existing SQLAlchemy model
from app.repositories.integration_repository import IntegrationRepository
from app.repositories.ticket_repository import TicketRepository
from app.repositories.agent_repository import AgentRepository

settings = get_settings()
logger = logging.getLogger(__name__)


class SyncResult:
    """Carries the outcome of a single integration sync."""

    def __init__(self, integration_id: str, crm_type: str) -> None:
        self.integration_id = integration_id
        self.crm_type = crm_type
        self.tickets_synced: int = 0
        self.agents_synced: int = 0
        self.organizations_synced: int = 0
        self.errors: List[str] = []
        self.started_at: datetime = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        duration = (
            (self.finished_at - self.started_at).total_seconds()
            if self.finished_at
            else "running"
        )
        return (
            f"SyncResult(integration={self.integration_id}, "
            f"crm={self.crm_type}, tickets={self.tickets_synced}, "
            f"agents={self.agents_synced}, success={self.success}, "
            f"duration={duration}s)"
        )


class CrmSyncService:
    """
    Orchestrates full CRM data syncs for all active integrations.

    Parameters
    ----------
    factory:
        The CrmAdapterFactory from app.state.  Injected — not constructed here.
    integration_repo:
        Repository for reading integration records from PostgreSQL.
    ticket_repo:
        Repository for upserting unified tickets into PostgreSQL.
    agent_repo:
        Repository for upserting unified agents into PostgreSQL.
    """

    def __init__(
        self,
        factory: CrmAdapterFactory,
        integration_repo: IntegrationRepository,
        ticket_repo: TicketRepository,
        agent_repo: AgentRepository,
    ) -> None:
        self._factory = factory
        self._integration_repo = integration_repo
        self._ticket_repo = ticket_repo
        self._agent_repo = agent_repo

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def sync_all_integrations(self) -> List[SyncResult]:
        """
        Fetch all active integrations from the DB and sync each one.
        Called by the scheduler and the manual /sync endpoint.

        Returns a list of SyncResult — one per integration.
        Errors in one integration never abort the others.
        """
        integrations = await self._integration_repo.get_all_active()
        logger.info("Starting full sync for %d active integrations.", len(integrations))

        results: List[SyncResult] = []
        for integration in integrations:
            result = await self.sync_integration(integration.integration_id)
            results.append(result)
            if result.success:
                logger.info("%s", result)
            else:
                logger.warning(
                    "Sync completed with errors for integration '%s': %s",
                    integration.integration_id,
                    result.errors,
                )

        return results

    async def sync_integration(self, integration_id: str) -> SyncResult:
        """
        Perform a full sync for a single integration.

        Builds the adapter via the factory, authenticates, then syncs
        tickets and agents.  All errors are captured into SyncResult
        so the caller decides how to handle them.

        Parameters
        ----------
        integration_id:
            The UUID stored in the integrations table.
        """
        # We need crm_type for the SyncResult before the adapter is open
        crm_type = await self._integration_repo.get_crm_type(integration_id) or "unknown"
        result = SyncResult(integration_id=integration_id, crm_type=crm_type)

        logger.info(
            "Syncing integration '%s' (crm_type=%s).", integration_id, crm_type
        )

        try:
            # ── Factory builds the full dependency graph ───────────────
            adapter = self._factory.create(integration_id)

            async with adapter:
                # ── Tickets ───────────────────────────────────────────
                tickets_synced = await self._sync_tickets(adapter, integration_id)
                result.tickets_synced = tickets_synced

                # ── Agents ────────────────────────────────────────────
                agents_synced = await self._sync_agents(adapter, integration_id)
                result.agents_synced = agents_synced

        except AdapterFactoryError as exc:
            msg = f"Factory failed to build adapter: {exc}"
            logger.error("[%s] %s", integration_id, msg)
            result.errors.append(msg)

        except CrmClientError as exc:
            msg = f"CRM HTTP error during sync: {exc}"
            logger.error("[%s] %s", integration_id, msg)
            result.errors.append(msg)

        except Exception as exc:
            msg = f"Unexpected error during sync: {exc}"
            logger.exception("[%s] %s", integration_id, msg)
            result.errors.append(msg)

        finally:
            result.finish()

        return result

    # ------------------------------------------------------------------
    # Private sync helpers — one per entity type
    # ------------------------------------------------------------------

    async def _sync_tickets(
        self, adapter: BaseCrmAdapter, integration_id: str
    ) -> int:
        """
        Page through all tickets from the adapter and upsert into the DB.
        Returns total count of tickets processed.
        """
        total = 0
        page = 1
        per_page = 100

        while True:
            page_result = await adapter.fetch_tickets(page=page, per_page=per_page)
            tickets: List[UnifiedTicket] = page_result.items

            if not tickets:
                break

            async with async_session_maker() as db:
                await self._ticket_repo.upsert_many(db, tickets)
                await db.commit()

            total += len(tickets)
            logger.debug(
                "[%s] Synced ticket page %d (%d items).",
                integration_id,
                page,
                len(tickets),
            )

            if not page_result.has_more:
                break
            page += 1

        logger.info("[%s] Ticket sync complete: %d total.", integration_id, total)
        return total

    async def _sync_agents(
        self, adapter: BaseCrmAdapter, integration_id: str
    ) -> int:
        """
        Page through all agents from the adapter and upsert into the DB.
        Returns total count of agents processed.
        """
        total = 0
        page = 1
        per_page = 100

        while True:
            page_result = await adapter.fetch_agents(page=page, per_page=per_page)
            agents: List[UnifiedAgent] = page_result.items

            if not agents:
                break

            async with async_session_maker() as db:
                await self._agent_repo.upsert_many(db, agents)
                await db.commit()

            total += len(agents)

            if not page_result.has_more:
                break
            page += 1

        logger.info("[%s] Agent sync complete: %d total.", integration_id, total)
        return total


# ---------------------------------------------------------------------------
# Feature-flag-aware factory function
# Used by scheduler.py to get the right sync implementation at runtime
# ---------------------------------------------------------------------------

def get_sync_service(factory: CrmAdapterFactory) -> CrmSyncService:
    """
    Construct a CrmSyncService with real repositories.

    Import this in scheduler.py:
        from app.services.crm_sync_service import get_sync_service
        service = get_sync_service(app.state.adapter_factory)
        await service.sync_all_integrations()
    """
    return CrmSyncService(
        factory=factory,
        integration_repo=IntegrationRepository(),
        ticket_repo=TicketRepository(),
        agent_repo=AgentRepository(),
    )


async def run_sync_with_feature_flag(factory: CrmAdapterFactory) -> List[SyncResult]:
    """
    Entry point that respects the CRM_ADAPTER_ENGINE feature flag.

    In scheduler.py replace:
        await run_all_tenants_full_sync()
    with:
        from app.services.crm_sync_service import run_sync_with_feature_flag
        await run_sync_with_feature_flag(app.state.adapter_factory)

    Environment variable:
        CRM_ADAPTER_ENGINE=legacy  → calls the old monolithic sync (unchanged)
        CRM_ADAPTER_ENGINE=new     → calls CrmSyncService (new adapter pattern)
    """
    engine = settings.CRM_ADAPTER_ENGINE  # add this to your Settings model + .env

    if engine == "new":
        logger.info("CRM_ADAPTER_ENGINE=new — using adapter pattern sync.")
        service = get_sync_service(factory)
        return await service.sync_all_integrations()
    else:
        logger.info("CRM_ADAPTER_ENGINE=legacy — using old sync path.")
        await run_all_tenants_full_sync()   # your existing function, unchanged
        return []