"""
app/integrations/webhooks/service.py

Receives RawWebhookPayload (plain dataclass, no ORM) + the open session
from the router. No new session opened here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.normalizer.espo_normalizer import normalize_espo_ticket
from app.integrations.normalizer.zammad_normalizer import normalize_zammad_ticket
from app.integrations.webhooks.models import RawWebhookPayload
from app.repositories.ticket_repository import TicketRepository
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)


async def handle_raw_webhook(
    payload: RawWebhookPayload,
    session: AsyncSession,
) -> None:
    """
    Entry point from router. Never raises — errors are logged so
    the router always returns 200 and CRMs never retry a valid delivery.
    """
    try:
        await _dispatch(payload, session)
    except Exception as exc:
        logger.exception(
            "Webhook processing failed | source=%s | event=%s | error=%s",
            payload.source_system,
            payload.event_type,
            exc,
        )


async def _dispatch(payload: RawWebhookPayload, session: AsyncSession) -> None:
    match payload.source_system:
        case "espocrm":
            await _handle_espo(payload, session)
        case "zammad":
            await _handle_zammad(payload, session)
        case _:
            logger.error("No handler for source_system=%s", payload.source_system)


# ── EspoCRM ────────────────────────────────────────────────────────────────────


async def _handle_espo(payload: RawWebhookPayload, session: AsyncSession) -> None:
    sync = SyncService(session)
    repo = TicketRepository(session)

    source_system_id = payload.source_system_id

    for raw in payload.records:
        crm_ticket_id = str(raw.get("id", ""))
        if not crm_ticket_id:
            logger.error("espo: record missing 'id' — skipping")
            continue

        try:
            match payload.event_type:
                case "Case.create":
                    await _espo_create(raw, source_system_id, sync, repo)
                case "Case.delete":
                    await _espo_delete(crm_ticket_id, source_system_id, repo)
                case _:
                    await _espo_partial_update(
                        crm_ticket_id, raw, source_system_id, sync, repo
                    )
        except Exception as exc:
            logger.exception(
                "espo: failed id=%s event=%s: %s",
                crm_ticket_id,
                payload.event_type,
                exc,
            )


async def _espo_create(
    raw: dict,
    source_system_id: int,
    sync: SyncService,
    repo: TicketRepository,
) -> None:
    try:
        normalized = normalize_espo_ticket(raw)
    except (KeyError, ValueError) as exc:
        logger.error("espo: normalisation failed id=%r: %s", raw.get("id"), exc)
        return

    status_id = await sync._get_status_id(normalized.status)
    if not status_id:
        logger.error("espo: cannot resolve status '%s' — skipping", normalized.status)
        return

    priority_id = await sync._get_priority_id(normalized.priority)
    agent_id = await sync._get_agent_uuid(normalized.crm_agent_id, source_system_id)
    customer_id = await sync._get_customer_uuid(
        normalized.crm_customer_id, source_system_id
    )
    company_id = await sync._get_company_uuid(
        normalized.crm_company_id, source_system_id
    )

    _, created = await repo.upsert(
        crm_ticket_id=normalized.crm_ticket_id,
        source_system_id=source_system_id,
        data={
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
    sync: SyncService,
    repo: TicketRepository,
) -> None:
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id)
    if not existing:
        logger.warning(
            "espo: Case.update id=%s not in DB — skipping, full sync will create it",
            crm_ticket_id,
        )
        return

    # Stale-update guard
    raw_modified = raw.get("modifiedAt")
    if raw_modified:
        try:
            incoming_ts = datetime.fromisoformat(raw_modified)
            if existing.updated_at and incoming_ts <= existing.updated_at:
                logger.info(
                    "espo: Case.update id=%s is stale — skipping", crm_ticket_id
                )
                return
        except (ValueError, TypeError):
            pass

    updates: dict = {}

    if "name" in raw:
        updates["title"] = (raw["name"] or "").strip() or "No Title"
    if "description" in raw:
        updates["description"] = raw.get("description")
    if "modifiedAt" in raw:
        try:
            updates["updated_at"] = datetime.fromisoformat(raw["modifiedAt"])
        except (ValueError, TypeError):
            pass

    if "status" in raw:
        status_id = await sync._get_status_id(str(raw["status"]).lower().strip())
        if status_id:
            updates["status_id"] = status_id
            if str(raw["status"]).lower().strip() in ("closed", "resolved"):
                updates["closed_at"] = updates.get("updated_at") or existing.updated_at
            elif existing.status_id != status_id:
                updates["closed_at"] = None
        else:
            logger.warning(
                "espo: cannot resolve status '%s' for id=%s",
                raw["status"],
                crm_ticket_id,
            )

    if "priority" in raw:
        updates["priority_id"] = await sync._get_priority_id(
            str(raw["priority"]).lower().strip()
        )

    if "assignedUserId" in raw:
        updates["agent_id"] = await sync._get_agent_uuid(
            raw["assignedUserId"] or None, source_system_id
        )

    if "createdById" in raw:
        updates["customer_id"] = await sync._get_customer_uuid(
            raw["createdById"] or None, source_system_id
        )

    if "accountId" in raw:
        updates["company_id"] = await sync._get_company_uuid(
            raw["accountId"] or None, source_system_id
        )

    if not updates:
        logger.info(
            "espo: Case.update id=%s — no recognised fields, nothing to update",
            crm_ticket_id,
        )
        return

    await repo.update(existing, updates)
    logger.info(
        "espo: Case.update id=%s — updated: %s",
        crm_ticket_id,
        list(updates.keys()),
    )


async def _espo_delete(
    crm_ticket_id: str,
    source_system_id: int,
    repo: TicketRepository,
) -> None:
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id)
    if not existing:
        logger.warning(
            "espo: Case.delete id=%s not in DB — nothing to delete", crm_ticket_id
        )
        return
    if existing.is_deleted:
        logger.info("espo: Case.delete id=%s — already soft-deleted", crm_ticket_id)
        return

    await repo.soft_delete(
        ticket=existing,
        deleted_by_id=None,
        deleted_by_source=True,
    )
    logger.info("espo: Case.delete id=%s — soft deleted", crm_ticket_id)


# ── Zammad ─────────────────────────────────────────────────────────────────────


async def _handle_zammad(payload: RawWebhookPayload, session: AsyncSession) -> None:
    sync = SyncService(session)
    repo = TicketRepository(session)

    source_system_id = payload.source_system_id

    for raw in payload.records:
        ticket_raw = raw.get("ticket", raw)
        crm_ticket_id = str(ticket_raw.get("id", ""))

        if not crm_ticket_id:
            logger.error("zammad: record missing 'id' — skipping")
            continue

        try:
            normalized = normalize_zammad_ticket(ticket_raw)
        except (KeyError, ValueError) as exc:
            logger.error("zammad: normalisation failed id=%s: %s", crm_ticket_id, exc)
            continue

        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            logger.error(
                "zammad: cannot resolve status '%s' for id=%s — skipping",
                normalized.status,
                crm_ticket_id,
            )
            continue

        priority_id = await sync._get_priority_id(normalized.priority)
        agent_id = await sync._get_agent_uuid(normalized.crm_agent_id, source_system_id)
        customer_id = await sync._get_customer_uuid(
            normalized.crm_customer_id, source_system_id
        )
        company_id = await sync._get_company_uuid(
            normalized.crm_company_id, source_system_id
        )

        _, created = await repo.upsert(
            crm_ticket_id=normalized.crm_ticket_id,
            source_system_id=source_system_id,
            data={
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
