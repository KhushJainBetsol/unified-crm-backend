"""
app/services/ticket_service.py

Business logic for tickets — sits between routes and repositories.

Responsibilities:
  - Source system resolution (name → DB row)
  - Agent existence validation
  - Filter orchestration (which repo method to call)
  - Stats queries (tenant-scoped)
  - get_or_404 helpers (tenant-scoped)
  - Role-gated ticket updates with CRM push (Zammad + EspoCRM)

Multitenancy:
  - Every public method accepts tenant_id: uuid.UUID | None.
  - It is passed down to the repository and to every raw SQL query in
    this file (stats). Never skip it in a multitenant request.

Routes should only call this service and return the response.
All DB-touching logic lives here or in the repository.

CRM push strategy:
  - DB is always updated first and committed.
  - CRM push is best-effort: failures are logged but never raised to the caller.
  - This means a CRM outage never rolls back a user's dashboard action.
  - If CRM push fails, investigate and re-sync manually.

Push mapping strategy:
  - All internal → CRM value mappings live in TOML files, not in this file.
  - Zammad : app/integrations/normalizer/config/zammad_mappings.toml
             [push_status] / [push_priority] / [pending_states]
  - EspoCRM: app/integrations/normalizer/config/espo_mappings.toml
             [push_status] / [push_priority]
  - Mappings are loaded once at import time. Restart the server after editing a TOML.

Pending state contract (Zammad-specific):
  - Zammad requires a `pending_time` timestamp in the PUT payload whenever the
    target state is listed in [pending_states] of zammad_mappings.toml.
  - The service trusts that TicketUpdateRequest has already been validated by
    its model_validator, so pending_until is guaranteed to be present when
    status == "pending" by the time this service is called.
  - pending_until is persisted to the DB and also forwarded to the CRM.
"""

from __future__ import annotations

import logging
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.espo.client import EspoClient
from app.integrations.espo.service import EspoService
from app.integrations.zammad.client import ZammadClient
from app.integrations.zammad.service import ZammadService
from app.models.agent import Agent
from app.models.source_system import SourceSystem
from app.models.ticket import Ticket
from app.models.ticket_priority import TicketPriority
from app.models.ticket_status import TicketStatus
from app.repositories.ticket_repository import TicketRepository
from app.schemas.ticket import TicketUpdateRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TOML config directory
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).parent.parent / "integrations/normalizer/config"


# ---------------------------------------------------------------------------
# TOML loaders
# ---------------------------------------------------------------------------


def _load_push_mappings(
    toml_filename: str,
    crm_name: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Load [push_status] and [push_priority] from a CRM mappings TOML file.

    Args:
        toml_filename: Filename only, e.g. "zammad_mappings.toml"
        crm_name:      Human-readable CRM name used in log messages.

    Returns:
        (push_status dict, push_priority dict)
        Both keyed by lowercase internal value → CRM display string.

    Raises:
        FileNotFoundError: if the TOML file does not exist.
        KeyError:          if [push_status] or [push_priority] sections are absent.
    """
    path = _CONFIG_DIR / toml_filename
    with open(path, "rb") as f:
        data = tomllib.load(f)

    push_status   = {k.lower(): v for k, v in data["push_status"].items()}
    push_priority = {k.lower(): v for k, v in data["push_priority"].items()}

    logger.info(
        "%s push mappings loaded — status: %s | priority: %s",
        crm_name,
        push_status,
        push_priority,
    )
    return push_status, push_priority


def _load_zammad_pending_states(toml_filename: str) -> frozenset[str]:
    """
    Load [pending_states].values from the Zammad mappings TOML.

    These are the exact Zammad state strings that require a `pending_time`
    timestamp in the PUT payload. Stored as a frozenset for O(1) membership
    checks and immutability.

    Returns:
        frozenset of lowercase Zammad state name strings.

    Raises:
        FileNotFoundError: if the TOML file does not exist.
        KeyError:          if [pending_states] section or `values` key is absent.
    """
    path = _CONFIG_DIR / toml_filename
    with open(path, "rb") as f:
        data = tomllib.load(f)

    states = frozenset(s.lower() for s in data["pending_states"]["values"])
    logger.info("Zammad pending states loaded: %s", states)
    return states


# ---------------------------------------------------------------------------
# Load all mappings once at import time.
# Failures degrade gracefully — CRM pushes are skipped with an error log
# rather than crashing the entire service on startup.
# ---------------------------------------------------------------------------

try:
    _ZAMMAD_STATUS, _ZAMMAD_PRIORITY = _load_push_mappings(
        "zammad_mappings.toml", "Zammad"
    )
except Exception as exc:
    logger.error(
        "Failed to load Zammad push mappings from zammad_mappings.toml: %s — "
        "CRM pushes for Zammad tickets will be skipped until this is fixed.",
        exc,
    )
    _ZAMMAD_STATUS   = {}
    _ZAMMAD_PRIORITY = {}

try:
    _ZAMMAD_PENDING_STATES: frozenset[str] = _load_zammad_pending_states(
        "zammad_mappings.toml"
    )
except Exception as exc:
    logger.error(
        "Failed to load Zammad pending_states from zammad_mappings.toml: %s — "
        "pending_time will never be sent in Zammad pushes until this is fixed.",
        exc,
    )
    _ZAMMAD_PENDING_STATES = frozenset()

try:
    _ESPO_STATUS, _ESPO_PRIORITY = _load_push_mappings("espo_mappings.toml", "EspoCRM")
except Exception as exc:
    logger.error(
        "Failed to load EspoCRM push mappings from espo_mappings.toml: %s — "
        "CRM pushes for EspoCRM tickets will be skipped until this is fixed.",
        exc,
    )
    _ESPO_STATUS   = {}
    _ESPO_PRIORITY = {}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TicketService:

    def __init__(self, db: AsyncSession) -> None:
        self.db   = db
        self.repo = TicketRepository(db)

    # ------------------------------------------------------------------
    # Source system helpers
    # ------------------------------------------------------------------

    async def _resolve_source_system(self, source: str) -> SourceSystem:
        """
        Resolve a source system name to its DB row.
        Raises HTTP 404 if not found.
        """
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.system_name == source.lower())
        )
        source_obj = result.scalars().first()
        if not source_obj:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Source system '{source}' not found. Valid values: zammad, espocrm",
            )
        return source_obj

    # ------------------------------------------------------------------
    # Agent validation helper
    # ------------------------------------------------------------------

    async def _get_agent_or_404(self, agent_id: uuid.UUID) -> Agent:
        """Fetch an agent by UUID or raise HTTP 404."""
        result = await self.db.execute(select(Agent).where(Agent.id == agent_id))
        agent  = result.scalars().first()
        if not agent:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent {agent_id} not found",
            )
        return agent

    # ------------------------------------------------------------------
    # Status / priority lookup helpers
    # ------------------------------------------------------------------

    async def _resolve_status(self, status_name: str) -> TicketStatus:
        """
        Resolve a status string to its DB row.
        Raises HTTP 422 if the value is not a known status.
        """
        result = await self.db.execute(
            select(TicketStatus).where(TicketStatus.status_name == status_name.lower())
        )
        obj = result.scalars().first()
        if not obj:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Invalid status '{status_name}'. "
                    "Valid values: open, pending, closed"
                ),
            )
        return obj

    async def _resolve_priority(self, priority_name: str) -> TicketPriority:
        """
        Resolve a priority string to its DB row.
        Raises HTTP 422 if the value is not a known priority.
        """
        result = await self.db.execute(
            select(TicketPriority).where(
                TicketPriority.priority_name == priority_name.lower()
            )
        )
        obj = result.scalars().first()
        if not obj:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Invalid priority '{priority_name}'. "
                    "Valid values: low, normal, high, urgent"
                ),
            )
        return obj

    # ------------------------------------------------------------------
    # List / filter
    # ------------------------------------------------------------------

    async def get_tickets(
        self,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """Return paginated list of all tickets for a tenant."""
        offset = (page - 1) * page_size
        return await self.repo.get_all(
            tenant_id=tenant_id,
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )

    async def filter_tickets(
        self,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        source: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int]:
        """Return paginated tickets with all optional filters applied."""
        offset = (page - 1) * page_size

        if source:
            source_obj = await self._resolve_source_system(source)
            return await self.repo.get_by_source_system(
                source_system_id=source_obj.id,
                tenant_id=tenant_id,
                include_deleted=include_deleted,
                status=status,
                priority=priority,
                offset=offset,
                limit=page_size,
            )

        return await self.repo.get_all(
            tenant_id=tenant_id,
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )

    async def get_tickets_by_agent(
        self,
        agent_id: uuid.UUID,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
        include_deleted: bool = False,
        status: str | None = None,
        priority: str | None = None,
    ) -> tuple[list, int, Agent]:
        """Validate agent exists, then return their tickets scoped to tenant."""
        agent  = await self._get_agent_or_404(agent_id)
        offset = (page - 1) * page_size
        tickets, total = await self.repo.get_by_agent(
            agent_id=agent_id,
            tenant_id=tenant_id,
            include_deleted=include_deleted,
            status=status,
            priority=priority,
            offset=offset,
            limit=page_size,
        )
        return tickets, total, agent

    # ------------------------------------------------------------------
    # Single ticket
    # ------------------------------------------------------------------

    async def get_ticket_or_404(
        self,
        ticket_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket:
        """
        Fetch a single ticket by UUID, scoped to tenant, or raise HTTP 404.
        Passing tenant_id ensures a ticket from another tenant returns 404
        rather than leaking data.
        """
        ticket = await self.repo.get_by_id(ticket_id, tenant_id=tenant_id)
        if not ticket:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_id} not found",
            )
        return ticket

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_ticket(
        self,
        ticket_id: uuid.UUID,
        update: TicketUpdateRequest,
        deleted_by_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> Ticket:
        """
        Apply a partial update to a ticket, then push the change to the
        originating CRM (best-effort — CRM failures are logged, not raised).

        Role gating is enforced upstream via `require_admin` on the route.

        The pending_until contract is enforced by TicketUpdateRequest's
        model_validator before this method is ever called:
          - status="pending"  → pending_until is always present here
          - status != "pending" → pending_until is always None here
        """
        ticket = await self.get_ticket_or_404(ticket_id, tenant_id=tenant_id)

        update_data: dict = {}

        if update.status is not None:
            status_obj = await self._resolve_status(update.status)
            update_data["status_id"] = status_obj.id
            new_status = update.status.lower()

            # --- closed_at lifecycle ---
            if new_status == "closed" and ticket.closed_at is None:
                update_data["closed_at"] = datetime.utcnow()
            elif new_status != "closed" and ticket.closed_at is not None:
                ticket.closed_at = None

            # --- pending_until lifecycle ---
            if new_status == "pending":
                update_data["pending_until"] = update.pending_until
            elif ticket.pending_until is not None:
                ticket.pending_until = None

        if update.priority is not None:
            priority_obj = await self._resolve_priority(update.priority)
            update_data["priority_id"] = priority_obj.id

        if update.agent_id is not None:
            await self._get_agent_or_404(update.agent_id)
            update_data["agent_id"] = update.agent_id

        if not update_data:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Request body contained no updatable fields",
            )

        updated_ticket = await self.repo.update(ticket, update_data)

        # Best-effort CRM push — never raises to the caller.
        await self._push_update_to_crm(updated_ticket, update)

        return updated_ticket

    # ------------------------------------------------------------------
    # CRM push — dispatcher
    # ------------------------------------------------------------------

    async def _push_update_to_crm(
        self,
        ticket: Ticket,
        payload: TicketUpdateRequest,
    ) -> None:
        """
        Route the update to the correct CRM based on the ticket's source system.
        Failures are caught and logged — they never bubble up to the caller
        because the DB update has already been committed at this point.
        """
        source = ticket.source_system.system_name.lower()
        try:
            if source == "zammad":
                await self._push_to_zammad(ticket, payload)
            elif source == "espocrm":
                await self._push_to_espo(ticket, payload)
            else:
                logger.warning(
                    "Ticket %s has unknown source system '%s' — skipping CRM push",
                    ticket.id,
                    source,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "CRM push failed for ticket %s (source=%s): %s — "
                "DB is already updated; investigate CRM sync manually.",
                ticket.id,
                source,
                exc,
            )

    # ------------------------------------------------------------------
    # CRM push — Zammad
    # ------------------------------------------------------------------

    async def _push_to_zammad(
        self,
        ticket: Ticket,
        payload: TicketUpdateRequest,
    ) -> None:
        """
        Map internal field names → Zammad field names (via TOML), validate
        against the instance's live state/priority endpoints, then PUT to Zammad.
        """
        async with ZammadClient() as client:
            service = ZammadService(client)

            # Fetch live field options for pre-validation.
            try:
                field_options    = await service.get_ticket_field_options()
                valid_states     = field_options.get("state", [])
                valid_priorities = field_options.get("priority", [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not fetch Zammad field options for ticket %s — "
                    "skipping pre-validation: %s",
                    ticket.id,
                    exc,
                )
                valid_states     = []
                valid_priorities = []

            crm_data: dict = {}

            # ---- Status ------------------------------------------------
            if payload.status is not None:
                mapped_status = _ZAMMAD_STATUS.get(payload.status.lower())

                if mapped_status is None:
                    logger.error(
                        "Zammad status mapping missing for ticket %s: "
                        "internal value '%s' has no entry in [push_status] of "
                        "zammad_mappings.toml. Valid keys: %s — skipping status field.",
                        ticket.id, payload.status, list(_ZAMMAD_STATUS.keys()),
                    )
                elif valid_states and mapped_status.lower() not in valid_states:
                    logger.error(
                        "Zammad state validation error for ticket %s: "
                        "mapped '%s' → '%s' but that state is not active on this instance. "
                        "Valid states: %s  "
                        "Update [push_status] in zammad_mappings.toml to fix this.",
                        ticket.id, payload.status, mapped_status, valid_states,
                    )
                else:
                    # Push by name — avoids instance-specific ID mismatches.
                    crm_data["state"] = mapped_status

                    if mapped_status.lower() in _ZAMMAD_PENDING_STATES:
                        if payload.pending_until is None:
                            logger.error(
                                "Zammad push for ticket %s: state '%s' requires "
                                "pending_time but pending_until is None — "
                                "dropping state field to avoid a malformed request. "
                                "This is a bug; TicketUpdateRequest validator should "
                                "have rejected this payload.",
                                ticket.id, mapped_status,
                            )
                            del crm_data["state"]
                        else:
                            # FIX 1: Ensure timezone info is present. Naive datetimes are 
                            # parsed as UTC by Zammad. If lacking tzinfo, append "Z" so 
                            # Zammad doesn't evaluate the local time as being in the past.
                            pt_str = payload.pending_until.isoformat()
                            if payload.pending_until.tzinfo is None:
                                pt_str += "Z"
                            
                            crm_data["pending_time"] = pt_str
                    else:
                        # FIX 2: Explicitly clear pending_time when moving to an open/closed state.
                        # Zammad will reject the transition if the old pending_time is in the past.
                        crm_data["pending_time"] = None
                        
            # FIX 3: Re-send pending_time if we aren't updating the status but the ticket is ALREADY pending.
            # Zammad runs full model validation on PUT. If we update priority on a pending ticket
            # whose pending_time has passed, Zammad rejects it with "Invalid value '3' for field 'state_id'!"
            elif ticket.status.status_name == "pending" and ticket.pending_until:
                pt_str = ticket.pending_until.isoformat()
                if ticket.pending_until.tzinfo is None:
                    pt_str += "Z"
                crm_data["pending_time"] = pt_str

            # ---- Priority ----------------------------------------------
            if payload.priority is not None:
                mapped_priority = _ZAMMAD_PRIORITY.get(payload.priority.lower())

                if mapped_priority is None:
                    logger.error(
                        "Zammad priority mapping missing for ticket %s: "
                        "internal value '%s' has no entry in [push_priority] of "
                        "zammad_mappings.toml. Valid keys: %s — skipping priority field.",
                        ticket.id, payload.priority, list(_ZAMMAD_PRIORITY.keys()),
                    )
                elif valid_priorities and mapped_priority not in valid_priorities:
                    logger.error(
                        "Zammad priority validation error for ticket %s: "
                        "mapped '%s' → '%s' but that priority is not active on this instance. "
                        "Valid priorities: %s  "
                        "Update [push_priority] in zammad_mappings.toml to fix this.",
                        ticket.id, payload.priority, mapped_priority, valid_priorities,
                    )
                else:
                    crm_data["priority"] = mapped_priority

            # ---- Agent (owner) -----------------------------------------
            if payload.agent_id is not None:
                agent        = await self._get_agent_or_404(payload.agent_id)
                crm_agent_id = getattr(agent, "crm_agent_id", None)
                if crm_agent_id:
                    crm_data["owner_id"] = int(crm_agent_id)
                else:
                    logger.warning(
                        "Agent %s has no crm_agent_id — "
                        "skipping owner_id in Zammad push for ticket %s",
                        payload.agent_id, ticket.id,
                    )

            if not crm_data:
                logger.debug(
                    "Zammad push skipped for ticket %s — no valid mapped fields",
                    ticket.id,
                )
                return

            await service.update_ticket(ticket.crm_ticket_id, crm_data)

        logger.info(
            "Zammad ticket %s updated successfully: %s",
            ticket.crm_ticket_id,
            crm_data,
        )

    # ------------------------------------------------------------------
    # CRM push — EspoCRM
    # ------------------------------------------------------------------

    async def _push_to_espo(
        self,
        ticket: Ticket,
        payload: TicketUpdateRequest,
    ) -> None:
        """
        Map internal field names → EspoCRM field names (via TOML), validate
        against the instance's live metadata, then PUT to EspoCRM.
        """
        async with EspoClient() as client:
            service = EspoService(client)

            try:
                field_options    = await service.get_case_field_options()
                valid_statuses   = field_options.get("status", [])
                valid_priorities = field_options.get("priority", [])
                logger.debug(
                    "EspoCRM Case field options — status: %s | priority: %s",
                    valid_statuses,
                    valid_priorities,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not fetch EspoCRM metadata for ticket %s — "
                    "skipping live validation (TOML validation still applies): %s",
                    ticket.id,
                    exc,
                )
                valid_statuses   = []
                valid_priorities = []

            crm_data: dict = {}

            if payload.status is not None:
                mapped_status = _ESPO_STATUS.get(payload.status.lower())
                if mapped_status is None:
                    logger.error(
                        "EspoCRM status mapping missing for ticket %s: "
                        "internal value '%s' has no entry in [push_status] of "
                        "espo_mappings.toml. Valid keys: %s — skipping status field.",
                        ticket.id, payload.status, list(_ESPO_STATUS.keys()),
                    )
                elif valid_statuses and mapped_status not in valid_statuses:
                    logger.error(
                        "EspoCRM status validation failed for ticket %s: "
                        "TOML maps '%s' → '%s' but EspoCRM rejects that value. "
                        "Valid EspoCRM status values for this instance: %s  "
                        "Update [push_status] in espo_mappings.toml to fix this.",
                        ticket.id, payload.status, mapped_status, valid_statuses,
                    )
                else:
                    crm_data["status"] = mapped_status

            if payload.priority is not None:
                mapped_priority = _ESPO_PRIORITY.get(payload.priority.lower())
                if mapped_priority is None:
                    logger.error(
                        "EspoCRM priority mapping missing for ticket %s: "
                        "internal value '%s' has no entry in [push_priority] of "
                        "espo_mappings.toml. Valid keys: %s — skipping priority field.",
                        ticket.id, payload.priority, list(_ESPO_PRIORITY.keys()),
                    )
                elif valid_priorities and mapped_priority not in valid_priorities:
                    logger.error(
                        "EspoCRM priority validation failed for ticket %s: "
                        "TOML maps '%s' → '%s' but EspoCRM rejects that value. "
                        "Valid EspoCRM priority values for this instance: %s  "
                        "Update [push_priority] in espo_mappings.toml to fix this.",
                        ticket.id, payload.priority, mapped_priority, valid_priorities,
                    )
                else:
                    crm_data["priority"] = mapped_priority

            if payload.agent_id is not None:
                agent        = await self._get_agent_or_404(payload.agent_id)
                crm_agent_id = getattr(agent, "crm_agent_id", None)
                if crm_agent_id:
                    crm_data["assignedUserId"] = str(crm_agent_id)
                else:
                    logger.warning(
                        "Agent %s has no crm_agent_id — "
                        "skipping assignedUserId in EspoCRM push for ticket %s",
                        payload.agent_id, ticket.id,
                    )

            if not crm_data:
                logger.debug(
                    "EspoCRM push skipped for ticket %s — no valid mapped fields",
                    ticket.id,
                )
                return

            await service.update_ticket(ticket.crm_ticket_id, crm_data)

        logger.info(
            "EspoCRM Case %s updated successfully: %s",
            ticket.crm_ticket_id,
            crm_data,
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self, tenant_id: uuid.UUID | None = None) -> dict:
        """
        Aggregate ticket counts for a tenant: total, active, deleted,
        by_status, by_priority.
        """
        base_filter = (
            [Ticket.tenant_id == tenant_id] if tenant_id is not None else []
        )

        result = await self.db.execute(
            select(
                func.count(Ticket.id).label("total"),
                func.sum(case((Ticket.is_deleted == False, 1), else_=0)).label("active"),  # noqa: E712
                func.sum(case((Ticket.is_deleted == True, 1), else_=0)).label("deleted"),   # noqa: E712
            ).where(*base_filter)
        )
        row = result.first()

        active_filter = [*base_filter, Ticket.is_deleted == False]  # noqa: E712

        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(*active_filter)
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(*active_filter)
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":          row.total or 0,
            "active":         row.active or 0,
            "deleted":        row.deleted or 0,
            "open":           by_status.get("open", 0),
            "closed":         by_status.get("closed", 0),
            "pending":        by_status.get("pending", 0),
            "high_priority":  by_priority.get("high", 0) + by_priority.get("urgent", 0),
            "by_status":      by_status,
            "by_priority":    by_priority,
        }

    async def get_agent_stats(
        self,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> dict:
        """
        Aggregate ticket counts for a specific agent, scoped to tenant.
        Raises HTTP 404 if agent doesn't exist.
        """
        await self._get_agent_or_404(agent_id)

        base_filter = [
            Ticket.agent_id  == agent_id,
            Ticket.is_deleted == False,  # noqa: E712
        ]
        if tenant_id is not None:
            base_filter.append(Ticket.tenant_id == tenant_id)

        total_result = await self.db.execute(
            select(func.count(Ticket.id)).where(*base_filter)
        )
        total = total_result.scalar_one()

        status_result = await self.db.execute(
            select(TicketStatus.status_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.status_id == TicketStatus.id)
            .where(*base_filter)
            .group_by(TicketStatus.status_name)
        )
        by_status = {r.status_name: r.count for r in status_result}

        priority_result = await self.db.execute(
            select(TicketPriority.priority_name, func.count(Ticket.id).label("count"))
            .join(Ticket, Ticket.priority_id == TicketPriority.id)
            .where(*base_filter)
            .group_by(TicketPriority.priority_name)
        )
        by_priority = {r.priority_name: r.count for r in priority_result}

        return {
            "total":         total,
            "open":          by_status.get("open", 0),
            "closed":        by_status.get("closed", 0),
            "pending":       by_status.get("pending", 0),
            "high_priority": by_priority.get("high", 0) + by_priority.get("urgent", 0),
            "by_status":     by_status,
            "by_priority":   by_priority,
        }