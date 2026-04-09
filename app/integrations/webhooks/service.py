"""
app/integrations/webhooks/service.py

Receives RawWebhookPayload (plain dataclass, no ORM) + the open session
from the router. No new session opened here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.normalizer.espo_normalizer import normalize_espo_ticket
from app.integrations.normalizer.zammad_normalizer import normalize_zammad_ticket
from app.integrations.webhooks.models import RawWebhookPayload
from app.repositories.ticket_repository import TicketRepository
from app.services.sync_service import SyncService

logger = logging.getLogger(__name__)


# ── Custom Exceptions ──────────────────────────────────────────────────────────


class WebhookProcessingError(Exception):
    """
    Raised when a webhook record fails processing in a way that is
    meaningful enough to log at ERROR level with full context.
    Wraps the original exception so the caller always has the root cause.
    """

    def __init__(self, message: str, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.original = original


class NormalizationError(WebhookProcessingError):
    """
    Raised when a raw CRM payload cannot be mapped to a normalised ticket.
    Use case: the CRM schema changed, a required field is missing, or a
    field contains an unexpected type (e.g. status is null).
    Separated from the generic error so callers can apply different retry
    or alerting logic in the future.
    """


class UnresolvableStatusError(WebhookProcessingError):
    """
    Raised when the status string from the CRM has no matching row in the
    statuses lookup table.
    Use case: a new status was added in the CRM but not yet mirrored in the
    unified app's configuration. This is a data/config error, not a code bug,
    so it deserves its own exception class to make alerting easier.
    """


# ── Entry point ────────────────────────────────────────────────────────────────


async def handle_raw_webhook(
    payload: RawWebhookPayload,
    session: AsyncSession,
) -> None:
    """
    Entry point from router. Never raises — errors are logged so
    the router always returns 200 and CRMs never retry a valid delivery.

    Why swallow all exceptions here?
    CRMs treat any non-2xx response as a failed delivery and will retry,
    often with exponential back-off. Retrying a malformed payload or an
    unknown source system will never succeed and wastes resources on both
    sides. We log everything we need for debugging and always ACK.
    """
    try:
        await _dispatch(payload, session)
    except WebhookProcessingError as exc:
        # Already logged with context where raised; log the summary here.
        logger.error(
            "Webhook processing error | source=%s | event=%s | reason=%s | original=%s",
            payload.source_system,
            payload.event_type,
            exc,
            exc.original,
        )
    except Exception as exc:
        # Truly unexpected — log the full traceback.
        logger.exception(
            "Unhandled exception during webhook processing | source=%s | event=%s",
            payload.source_system,
            payload.event_type,
            exc_info=exc,
        )


async def _dispatch(payload: RawWebhookPayload, session: AsyncSession) -> None:
    """
    Routes the payload to the correct CRM handler.

    Why a match statement instead of a dict of callables?
    Each handler has a different signature and setup needs. A match keeps
    the branching explicit and readable; new CRMs are added in one place.
    """
    match payload.source_system:
        case "espocrm":
            await _handle_espo(payload, session)
        case "zammad":
            await _handle_zammad(payload, session)
        case _:
            # Not a retryable error — the source_system value is baked into
            # the integration record, so retrying will produce the same result.
            logger.error(
                "No handler registered for source_system=%s — "
                "check the CrmIntegration configuration",
                payload.source_system,
            )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _extract_record_id(raw: dict[str, Any], source: str, id_key: str = "id") -> str | None:
    """
    Safely extracts and validates the CRM record ID from a raw payload dict.

    Why a helper?
    Both EspoCRM and Zammad records must have an ID to be processable. The
    extraction + validation logic is identical; centralising it avoids
    drift between the two handlers and makes testing straightforward.

    Returns None (instead of raising) so the caller can log and skip
    without interrupting the rest of the batch.
    """
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
    """
    Attempts to parse an ISO-8601 timestamp string, logging a warning on failure.

    Why a helper?
    Timestamp parsing appears in both the stale-update guard and in field
    mapping. Using a shared helper ensures consistent warning messages and
    avoids silent data loss — if parsing fails we return None rather than
    crashing the whole record.
    """
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


async def _handle_espo(payload: RawWebhookPayload, session: AsyncSession) -> None:
    """
    Fans out per-record processing for EspoCRM events.

    Why per-record try/except?
    A batch webhook can contain multiple records. One malformed record must
    not abort processing for the rest of the batch. Each failure is logged
    independently so ops can replay or fix only the affected record.
    """
    sync = SyncService(session)
    repo = TicketRepository(session)
    source_system_id = payload.source_system_id

    for raw in payload.records:
        crm_ticket_id = _extract_record_id(raw, source="espo")
        if crm_ticket_id is None:
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
        except (NormalizationError, UnresolvableStatusError) as exc:
            # Config/data errors: log clearly, no stack trace needed.
            logger.error(
                "espo: skipping id=%s event=%s — %s",
                crm_ticket_id,
                payload.event_type,
                exc,
            )
        except Exception as exc:
            # Unexpected errors: full traceback for debugging.
            logger.exception(
                "espo: unexpected error id=%s event=%s",
                crm_ticket_id,
                payload.event_type,
                exc_info=exc,
            )


async def _espo_create(
    raw: dict,
    source_system_id: int,
    sync: SyncService,
    repo: TicketRepository,
) -> None:
    """
    Handles Case.create events from EspoCRM.

    Why raise NormalizationError instead of returning early?
    Returning early silently drops the record. Raising a typed exception
    lets the caller (_handle_espo) decide how to handle it — currently it
    logs at ERROR, but in the future it could dead-letter the record or
    trigger an alert, without changing this function.
    """
    try:
        normalized = normalize_espo_ticket(raw)
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
    """
    Handles Case.update (and any unrecognised) events from EspoCRM.

    Stale-update guard: if the incoming modifiedAt is not newer than
    what we have stored, the update is silently skipped. This prevents
    out-of-order webhook deliveries from overwriting newer data.

    Why warn (not error) when the ticket is missing?
    A missing ticket on update most likely means the create webhook was
    dropped or processed out of order. It is a recoverable gap — the next
    full sync will fill it in. ERROR would create alert noise for a
    transient condition.
    """
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id)
    if not existing:
        logger.warning(
            "espo: Case.update id=%s not in DB — "
            "skipping; full sync will create it",
            crm_ticket_id,
        )
        return

    # Stale-update guard
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
            # Warn, not error — the rest of the update is still valid.
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
        "espo: Case.update id=%s — updated fields: %s",
        crm_ticket_id,
        list(updates.keys()),
    )


async def _espo_delete(
    crm_ticket_id: str,
    source_system_id: int,
    repo: TicketRepository,
) -> None:
    """
    Handles Case.delete events from EspoCRM.

    Why treat 'already soft-deleted' as INFO, not a warning?
    Duplicate delete webhooks are common — the CRM may retry or fire
    the event multiple times. Idempotent handling (skip if already deleted)
    is correct behaviour, not an anomaly worth surfacing in alerts.
    """
    existing = await repo.get_by_crm_id(crm_ticket_id, source_system_id)
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
        deleted_by_source=True,
    )
    logger.info("espo: Case.delete id=%s — soft deleted", crm_ticket_id)


# ── Zammad ─────────────────────────────────────────────────────────────────────


async def _handle_zammad(payload: RawWebhookPayload, session: AsyncSession) -> None:
    """
    Fans out per-record processing for Zammad events.

    Why extract ticket_raw = raw.get("ticket", raw)?
    Zammad wraps the ticket object under a "ticket" key in some webhook
    shapes but sends it flat in others. Falling back to raw itself handles
    both shapes without requiring separate handlers.

    Why raise NormalizationError / UnresolvableStatusError up to here?
    These are config/data issues. Logging them at ERROR here (rather than
    letting them propagate to handle_raw_webhook) gives us per-record
    context (crm_ticket_id, event) in the log line, which is far more
    useful when debugging a specific delivery failure.
    """
    sync = SyncService(session)
    repo = TicketRepository(session)
    source_system_id = payload.source_system_id

    for raw in payload.records:
        ticket_raw = raw.get("ticket", raw)
        crm_ticket_id = _extract_record_id(ticket_raw, source="zammad")
        if crm_ticket_id is None:
            continue

        try:
            normalized = normalize_zammad_ticket(ticket_raw)
        except (KeyError, ValueError) as exc:
            raise NormalizationError(
                f"zammad: normalisation failed for id={crm_ticket_id}", original=exc
            ) from exc

        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            raise UnresolvableStatusError(
                f"zammad: cannot resolve status={normalized.status!r} for id={crm_ticket_id}"
            )

        try:
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