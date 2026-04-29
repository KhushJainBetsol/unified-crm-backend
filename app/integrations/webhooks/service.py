"""
app/integrations/webhooks/service.py
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.normalizer.normalizer import normalize_ticket, normalize_tickets
from app.config.registry import AdapterRegistry
from app.config.models import AdapterConfig
from app.integrations.webhooks.models import RawWebhookPayload
from app.repositories.ticket_repository import TicketRepository
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)


# ── Custom Exceptions ──────────────────────────────────────────────────────────


class WebhookProcessingError(Exception):
    def __init__(self, message: str, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.original = original


class NormalizationError(WebhookProcessingError):
    pass


class UnresolvableStatusError(WebhookProcessingError):
    pass


# ── Entry point ────────────────────────────────────────────────────────────────


async def handle_raw_webhook(
    payload: RawWebhookPayload,
    session: AsyncSession,
) -> None:
    try:
        await _dispatch(payload, session)
    except WebhookProcessingError as exc:
        logger.error(
            "Webhook processing error | source=%s | event=%s | reason=%s | original=%s",
            payload.source_system,
            payload.event_type,
            exc,
            exc.original,
        )
    except Exception as exc:
        logger.exception(
            "Unhandled exception during webhook processing | source=%s | event=%s",
            payload.source_system,
            payload.event_type,
            exc_info=exc,
        )


async def _dispatch(payload: RawWebhookPayload, session: AsyncSession) -> None:
    """
    Routes the payload to the correct CRM handler.

    Loads the AdapterConfig from the central AdapterRegistry.
    If the source_system is not registered, early-exit with a log message.

    This ensures webhook processing uses the same config as the adapter layer,
    avoiding duplicate config mappings.
    """
    from app.adapter_dependencies.deps import get_adapter_factory_instance

    try:
        factory = get_adapter_factory_instance()
        registry = factory._adapter_registry
    except RuntimeError as exc:
        logger.error("Adapter registry not initialized: %s", exc)
        return

    # Validate that the source system is registered
    try:
        config = registry.get_adapter_config(payload.source_system)
    except Exception as exc:
        logger.error(
            "Source system not registered | source=%s | error=%s",
            payload.source_system,
            exc,
        )
        return

    # Route to the appropriate handler
    if payload.source_system == "espocrm":
        await _handle_espo(payload, session, config)
    elif payload.source_system == "zammad":
        await _handle_zammad(payload, session, config)
    else:
        logger.error(
            "No handler registered for source_system=%s",
            payload.source_system,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _extract_record_id(raw: dict[str, Any], source: str, id_key: str = "id") -> str | None:
    crm_id = str(raw.get(id_key, "")).strip()
    if not crm_id:
        logger.error(
            "%s: record missing or empty '%s' field — skipping | raw_keys=%s",
            source,
            id_key,
            list(raw.keys()),
        )
        return None
    return crm_id


def _parse_iso_timestamp(value: Any, field_name: str, context: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        logger.warning(
            "%s: could not parse %s=%r — %s", context, field_name, value, exc
        )
        return None


# ── EspoCRM ────────────────────────────────────────────────────────────────────


async def _handle_espo(
    payload: RawWebhookPayload,
    session: AsyncSession,
    config: AdapterConfig,      # FIX: was missing; config was undefined in callee scope
) -> None:
    sync = SyncService(session)
    repo = TicketRepository(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    for raw in payload.records:
        crm_ticket_id = _extract_record_id(raw, source="espo")
        if crm_ticket_id is None:
            continue

        try:
            if payload.event_type == "Case.create":
                await _espo_create(raw, source_system_id, tenant_id, sync, repo, config)
            elif payload.event_type == "Case.delete":
                await _espo_delete(crm_ticket_id, source_system_id, tenant_id, repo)
            else:
                await _espo_partial_update(
                    crm_ticket_id, raw, source_system_id, tenant_id, sync, repo
                )
        except (NormalizationError, UnresolvableStatusError) as exc:
            logger.error(
                "espo: skipping id=%s event=%s — %s",
                crm_ticket_id,
                payload.event_type,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "espo: unexpected error id=%s event=%s",
                crm_ticket_id,
                payload.event_type,
                exc_info=exc,
            )


async def _espo_create(
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID | None,
    sync: SyncService,
    repo: TicketRepository,
    config: AdapterConfig,      # FIX: added — was undefined at call site
) -> None:
    try:
        # FIX: was normalize_tickets (batch function) called on a single dict.
        # normalize_ticket returns one NormalizedTicket; normalize_tickets
        # expects a list[dict] and returns list[NormalizedTicket].
        normalized = normalize_ticket(raw, "espocrm", config)
    except (KeyError, ValueError) as exc:
        raise NormalizationError(
            f"espo: normalisation failed for id={raw.get('id')!r}", original=exc
        ) from exc

    status_id = await sync._get_status_id(normalized.status)
    if not status_id:
        raise UnresolvableStatusError(
            f"espo: cannot resolve status={normalized.status!r} for id={normalized.crm_ticket_id}"
        )

    priority_id = await sync._get_priority_id(normalized.priority)
    agent_id = await sync._get_agent_uuid(normalized.crm_agent_id, source_system_id, tenant_id)
    customer_id = await sync._get_customer_uuid(
        normalized.crm_customer_id, source_system_id, tenant_id
    )
    company_id = await sync._get_company_uuid(
        normalized.crm_company_id, source_system_id, tenant_id
    )

    _, created = await repo.upsert(
        crm_ticket_id=normalized.crm_ticket_id,
        source_system_id=source_system_id,
        tenant_id=tenant_id,
        data={
            "tenant_id": tenant_id,
            "title": normalized.title,
            "description": normalized.description,
            "status_id": status_id,
            "priority_id": priority_id,
            "agent_id": agent_id,
            "customer_id": customer_id,
            "company_id": company_id,
            "created_at": normalized.created_at,
            "updated_at": normalized.updated_at,
            "closed_at": normalized.closed_at,
            "is_deleted": False,
            "deleted_by_source": False,
        },
    )
    logger.info(
        "espo: Case.create id=%s — %s",
        normalized.crm_ticket_id,
        "inserted" if created else "already existed, updated",
    )


async def _espo_partial_update(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID | None,
    sync: SyncService,
    repo: TicketRepository,
) -> None:
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id, tenant_id=tenant_id)
    if not existing:
        logger.warning(
            "espo: Case.update id=%s not in DB — "
            "skipping; full sync will create it",
            crm_ticket_id,
        )
        return

    incoming_ts = _parse_iso_timestamp(
        raw.get("modifiedAt"), "modifiedAt", f"espo id={crm_ticket_id}"
    )
    if incoming_ts and existing.updated_at and incoming_ts <= existing.updated_at:
        logger.info(
            "espo: Case.update id=%s is stale (incoming=%s <= stored=%s) — skipping",
            crm_ticket_id,
            incoming_ts,
            existing.updated_at,
        )
        return

    updates: dict = {}

    if "name" in raw:
        updates["title"] = (raw["name"] or "").strip() or "No Title"
    if "description" in raw:
        updates["description"] = raw.get("description")
    if "modifiedAt" in raw:
        ts = _parse_iso_timestamp(raw["modifiedAt"], "modifiedAt", f"espo id={crm_ticket_id}")
        if ts:
            updates["updated_at"] = ts

    if "status" in raw:
        status_str = str(raw["status"]).lower().strip()
        status_id = await sync._get_status_id(status_str)
        if status_id:
            updates["status_id"] = status_id
            if status_str in ("closed", "resolved"):
                updates["closed_at"] = updates.get("updated_at") or existing.updated_at
            elif existing.status_id != status_id:
                updates["closed_at"] = None
        else:
            logger.warning(
                "espo: cannot resolve status=%r for id=%s — status field skipped",
                raw["status"],
                crm_ticket_id,
            )

    if "priority" in raw:
        updates["priority_id"] = await sync._get_priority_id(
            str(raw["priority"]).lower().strip()
        )

    if "assignedUserId" in raw:
        updates["agent_id"] = await sync._get_agent_uuid(
            raw["assignedUserId"] or None, source_system_id, tenant_id
        )

    if "createdById" in raw:
        updates["customer_id"] = await sync._get_customer_uuid(
            raw["createdById"] or None, source_system_id, tenant_id
        )

    if "accountId" in raw:
        updates["company_id"] = await sync._get_company_uuid(
            raw["accountId"] or None, source_system_id, tenant_id
        )

    if not updates:
        logger.info(
            "espo: Case.update id=%s — no recognised fields, nothing to update",
            crm_ticket_id,
        )
        return

    await repo.update(existing, updates)
    logger.info(
        "espo: Case.update id=%s — updated fields: %s",
        crm_ticket_id,
        list(updates.keys()),
    )


async def _espo_delete(
    crm_ticket_id: str,
    source_system_id: int,
    tenant_id: uuid.UUID | None,
    repo: TicketRepository,
) -> None:
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id, tenant_id=tenant_id)
    if not existing:
        logger.warning(
            "espo: Case.delete id=%s not in DB — nothing to delete",
            crm_ticket_id,
        )
        return
    if existing.is_deleted:
        logger.info(
            "espo: Case.delete id=%s — already soft-deleted, idempotent skip",
            crm_ticket_id,
        )
        return

    await repo.soft_delete(
        ticket=existing,
        deleted_by_id=None,
        is_deleted_by_crm=True,
    )
    logger.info("espo: Case.delete id=%s — soft deleted", crm_ticket_id)


# ── Zammad ─────────────────────────────────────────────────────────────────────


async def _handle_zammad(
    payload: RawWebhookPayload,
    session: AsyncSession,
    config: AdapterConfig,      # FIX: was missing; config was undefined in callee scope
) -> None:
    sync = SyncService(session)
    repo = TicketRepository(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    for raw in payload.records:
        ticket_raw = raw.get("ticket", raw)
        crm_ticket_id = _extract_record_id(ticket_raw, source="zammad")
        if crm_ticket_id is None:
            continue

        try:
            try:
                # FIX: was normalize_zammad_ticket(ticket_raw) — that function was
                # deleted when the normalizers were consolidated. Use the unified
                # normalize_ticket, passing the loaded AdapterConfig.
                normalized = normalize_ticket(ticket_raw, "zammad", config)
            except (KeyError, ValueError) as exc:
                raise NormalizationError(
                    f"zammad: normalisation failed for id={crm_ticket_id}", original=exc
                ) from exc

            status_id = await sync._get_status_id(normalized.status)
            if not status_id:
                raise UnresolvableStatusError(
                    f"zammad: cannot resolve status={normalized.status!r} for id={crm_ticket_id}"
                )

            priority_id = await sync._get_priority_id(normalized.priority)
            agent_id = await sync._get_agent_uuid(
                normalized.crm_agent_id, source_system_id, tenant_id
            )
            customer_id = await sync._get_customer_uuid(
                normalized.crm_customer_id, source_system_id, tenant_id
            )
            company_id = await sync._get_company_uuid(
                normalized.crm_company_id, source_system_id, tenant_id
            )

            _, created = await repo.upsert(
                crm_ticket_id=normalized.crm_ticket_id,
                source_system_id=source_system_id,
                tenant_id=tenant_id,
                data={
                    "tenant_id": tenant_id,
                    "title": normalized.title,
                    "description": normalized.description,
                    "status_id": status_id,
                    "priority_id": priority_id,
                    "agent_id": agent_id,
                    "customer_id": customer_id,
                    "company_id": company_id,
                    "created_at": normalized.created_at,
                    "updated_at": normalized.updated_at,
                    "closed_at": normalized.closed_at,
                    "is_deleted": False,
                    "deleted_by_source": False,
                },
            )
            logger.info(
                "zammad: event=%s id=%s — %s",
                payload.event_type,
                crm_ticket_id,
                "inserted" if created else "updated",
            )
        except (NormalizationError, UnresolvableStatusError) as exc:
            logger.error(
                "zammad: skipping id=%s event=%s — %s",
                crm_ticket_id,
                payload.event_type,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "zammad: unexpected error id=%s event=%s",
                crm_ticket_id,
                payload.event_type,
                exc_info=exc,
            )