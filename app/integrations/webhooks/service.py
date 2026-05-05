"""
app/integrations/webhooks/service.py

Webhook processing service — adapter pattern, SyncService, comprehensive
error handling.  All secrets come from CrmIntegration.

Bugs fixed
----------
1. Zammad event_type never matched "create"/"update" — ZammadWebhookHandler
   ._extract_event() already normalises the raw Zammad event string to
   internal keys ("create" / "update" / "delete").

2. EspoCRM comments — handler matches both "Note.create" (what EspoCRM
   actually sends) and "Comment.create" for forward-compatibility.

3. _create_adapter received outbound_credentials dict instead of the
   integration_id string, so factory.create("") was always called and
   silently failed. integration_id is now passed explicitly from the payload.

4. _get_adapter_registry() accessed factory._adapter_registry — a private
   attribute that does NOT exist on CrmAdapterFactory.

   Root cause (confirmed from adapter_factory.py):
     CrmAdapterFactory stores the registry as self._registry (not
     self._adapter_registry), and exposes the config via:
         self._registry.get_adapter_config(crm_type)
     The old helper called factory._adapter_registry which raised
     AttributeError on every Case.create / Zammad create, causing every
     ticket creation webhook to be silently discarded.

   Fix: _get_adapter_config(source_system) now calls
       factory._registry.get_adapter_config(source_system)
   exactly as CrmAdapterFactory.create() does internally.

5. EspoCRM Case.delete event_type defaulted to "unknown" when the
   X-Webhook-Event header was absent. EspoCRM sends ONLY {"id": "..."}
   for delete events — no "deleted" flag, no timestamps — so the previous
   _infer_event_type() never matched and soft_delete() was never called.

   Fix (two-layer defence):
   Layer 1 — espo.py _infer_event_type() now detects the minimal payload
             {"id": "..."} as Case.delete (no create/update fields present).
   Layer 2 — _espo_attempt_delete_fallback() runs when event_type is still
             "unknown" after parsing: it re-checks each record for the same
             minimal pattern and calls _espo_delete_ticket() directly.
             This catches deliveries where the header is absent AND inference
             failed for any unforeseen reason.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base.adapter import AuthenticationError, AdapterError
from app.factory.adapter_factory import CrmAdapterFactory, AdapterFactoryError
from app.integrations.normalizer.normalizer import normalize_ticket
from app.integrations.webhooks.errors import (
    WebhookAdapterError,
    WebhookCommentError,
    WebhookSyncError,
)
from app.integrations.webhooks.models import RawWebhookPayload
from app.repositories.ticket_repository import TicketRepository
from app.services.sync_service import SyncService
from app.utils.retry import retry_on_conflict

logger = logging.getLogger(__name__)

# Fields that are present on EspoCRM create/update payloads but absent from
# delete payloads. Mirrors the constant in espo.py — duplicated here so
# service.py has no import dependency on the handler layer.
_ESPO_CREATE_UPDATE_INDICATOR_FIELDS = frozenset(
    {
        "createdAt",
        "modifiedAt",
        "name",
        "status",
        "parentType",
        "description",
        "assignedUserId",
        "accountId",
    }
)


# ── Entry point ────────────────────────────────────────────────────────────────


async def handle_raw_webhook(
    payload: RawWebhookPayload,
    session: AsyncSession,
    webhook_secrets: Dict[str, Any] = None,
    outbound_credentials: Dict[str, Any] = None,
) -> None:
    """
    Main webhook handler — processes a verified, parsed webhook payload.

    Never raises to caller — all errors are logged and swallowed so the
    router always returns 200 ACK and the CRM does not retry valid deliveries.
    """
    try:
        await _dispatch(
            payload,
            session,
            webhook_secrets=webhook_secrets,
            outbound_credentials=outbound_credentials,
        )
    except Exception as exc:
        logger.exception(
            "Unhandled exception during webhook processing | "
            "webhook_uuid=%s | source=%s | event=%s | integration_id=%s",
            payload.webhook_uuid,
            payload.source_system,
            payload.event_type,
            payload.integration_id,
            exc_info=exc,
        )


async def _dispatch(
    payload: RawWebhookPayload,
    session: AsyncSession,
    webhook_secrets: Dict[str, Any] = None,
    outbound_credentials: Dict[str, Any] = None,
) -> None:
    """Routes payload to the appropriate CRM handler."""
    if payload.source_system == "espocrm":
        await _handle_espo(payload, session, webhook_secrets, outbound_credentials)
    elif payload.source_system == "zammad":
        await _handle_zammad(payload, session, webhook_secrets, outbound_credentials)
    else:
        logger.error(
            "No handler registered for source_system=%s | webhook_uuid=%s",
            payload.source_system,
            payload.webhook_uuid,
        )


# ── EspoCRM Handler ────────────────────────────────────────────────────────────


async def _handle_espo(
    payload: RawWebhookPayload,
    session: AsyncSession,
    webhook_secrets: Dict[str, Any] = None,
    outbound_credentials: Dict[str, Any] = None,
) -> None:
    """
    EspoCRM webhook handler.

    Supported event types:
      Case.create    → upsert ticket
      Case.update    → partial update ticket
      Case.delete    → soft delete ticket
      Note.create    → fetch + persist comments
      Comment.create → alias for Note.create (forward-compat)
      unknown        → attempt delete fallback before warning

    Unknown-event fallback
    ----------------------
    When event_type is "unknown" (header absent + inference failed),
    _espo_attempt_delete_fallback() is called. It checks each record for
    the EspoCRM delete payload signature — {"id": "..."} with no create/update
    fields — and soft-deletes any matches. This is a safety net for the
    case where both the X-Webhook-Event header and payload inference fail.
    """
    sync = SyncService(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    # ── Unknown-event fallback (runs before the per-record loop) ──────────
    # When event_type could not be resolved, attempt a delete based on payload
    # shape before giving up. This handles the case where the X-Webhook-Event
    # header is absent AND _infer_event_type() returned None (e.g. an entirely
    # empty record, or a shape we haven't seen before).
    if payload.event_type == "unknown":
        await _espo_attempt_delete_fallback(payload, session)
        return

    # ── Per-record processing for all other known event types ─────────────
    for raw in payload.records:
        crm_ticket_id = _extract_record_id(raw, source="espocrm")
        if crm_ticket_id is None:
            continue

        try:
            if payload.event_type == "Case.create":
                await _espo_create_ticket(
                    raw, source_system_id, tenant_id, sync
                )
            elif payload.event_type == "Case.update":
                await _espo_update_ticket(
                    crm_ticket_id, raw, source_system_id, tenant_id, sync
                )
            elif payload.event_type == "Case.delete":
                await _espo_delete_ticket(
                    crm_ticket_id, source_system_id, tenant_id, session
                )
            elif payload.event_type in ("Note.create", "Comment.create"):
                await _espo_create_comment(
                    crm_ticket_id,
                    raw,
                    source_system_id,
                    tenant_id,
                    session,
                    integration_id=payload.integration_id,
                )
            else:
                logger.warning(
                    "espocrm: unhandled event_type=%s | webhook_uuid=%s",
                    payload.event_type,
                    payload.webhook_uuid,
                )
        except WebhookSyncError as exc:
            logger.error(
                "espocrm: sync failed | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid, exc,
            )
        except WebhookAdapterError as exc:
            logger.error(
                "espocrm: adapter error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid, exc,
            )
        except WebhookCommentError as exc:
            logger.error(
                "espocrm: comment error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid, exc,
            )
        except Exception as exc:
            logger.exception(
                "espocrm: unexpected error | id=%s | event=%s | webhook_uuid=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid,
                exc_info=exc,
            )


async def _espo_attempt_delete_fallback(
    payload: RawWebhookPayload,
    session: AsyncSession,
) -> None:
    """
    Fallback delete handler for EspoCRM payloads where event_type is "unknown".

    When EspoCRM sends a Case.delete webhook WITHOUT the X-Webhook-Event
    header and _infer_event_type() also fails (e.g. the parser ran before
    this fix was deployed, or a future EspoCRM version changes the shape),
    this function provides a last line of defence.

    Detection logic
    ---------------
    A record is treated as a delete candidate when:
      - It has an "id" field (required to look up the ticket in our DB), AND
      - It has NONE of the fields that always appear on create/update payloads
        (createdAt, modifiedAt, name, status, parentType, description,
         assignedUserId, accountId).

    This matches the confirmed EspoCRM delete payload shape:
        {"id": "69f504798ae7eff4b"}

    Non-delete records (create/update payloads arriving as "unknown") will
    have at least one of the indicator fields and will NOT be soft-deleted,
    so this fallback is safe to run even on ambiguous deliveries.

    Outcomes
    --------
    - Delete candidate found + ticket exists in DB  → soft_delete() called,
      INFO log with crm_ticket_id.
    - Delete candidate found + ticket not in DB     → warning logged (already
      handled by _espo_delete_ticket).
    - No delete candidates found in payload         → WARNING logged with
      record keys so engineers can diagnose the unexpected shape.
    - Per-record delete error                       → ERROR logged, other
      records in the same delivery continue processing.
    """
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id
    deleted_count = 0
    candidate_count = 0

    for raw in payload.records:
        record_keys = set(raw.keys())

        # Detect minimal delete payload: has "id", lacks all create/update fields.
        is_delete_candidate = (
            "id" in record_keys
            and not record_keys.intersection(_ESPO_CREATE_UPDATE_INDICATOR_FIELDS)
        )

        if not is_delete_candidate:
            logger.warning(
                "espocrm: fallback | record not a delete candidate | "
                "record_keys=%s | webhook_uuid=%s | "
                "this delivery may be a create/update with a missing header — "
                "add X-Webhook-Event in EspoCRM webhook config to fix permanently",
                sorted(record_keys),
                payload.webhook_uuid,
            )
            continue

        candidate_count += 1
        crm_ticket_id = _extract_record_id(raw, source="espocrm")
        if not crm_ticket_id:
            continue

        logger.info(
            "espocrm: fallback delete triggered | crm_ticket_id=%s | "
            "record_keys=%s | webhook_uuid=%s | integration_id=%s",
            crm_ticket_id,
            sorted(record_keys),
            payload.webhook_uuid,
            payload.integration_id,
        )

        try:
            await _espo_delete_ticket(
                crm_ticket_id, source_system_id, tenant_id, session
            )
            deleted_count += 1
        except WebhookSyncError as exc:
            logger.error(
                "espocrm: fallback delete failed | crm_ticket_id=%s | "
                "webhook_uuid=%s | reason=%s",
                crm_ticket_id,
                payload.webhook_uuid,
                exc,
            )

    # Summary log — always emit so monitoring alerts can key on this line.
    if candidate_count == 0:
        logger.warning(
            "espocrm: fallback found no delete candidates | "
            "webhook_uuid=%s | integration_id=%s | "
            "total_records=%d | all record_keys=%s | "
            "delivery dropped — add X-Webhook-Event header to prevent this",
            payload.webhook_uuid,
            payload.integration_id,
            len(payload.records),
            [sorted(r.keys()) for r in payload.records if isinstance(r, dict)],
        )
    else:
        logger.info(
            "espocrm: fallback complete | candidates=%d | soft_deleted=%d | "
            "webhook_uuid=%s | integration_id=%s",
            candidate_count,
            deleted_count,
            payload.webhook_uuid,
            payload.integration_id,
        )


async def _espo_create_ticket(
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """
    EspoCRM Case.create — normalize raw webhook payload and persist via SyncService.

    Safety check: if ticket exists but is soft-deleted, skip to prevent resurrection.

    Normalization uses the AdapterConfig fetched from factory._registry
    (the same config object injected into BaseCrmAdapter and SchemaMapper),
    so status_map / priority_map / field mappings are always consistent
    with the adapter layer.

    Flow
    ----
    1. _get_adapter_config("espocrm") → AdapterConfig  (from factory._registry)
    2. normalize_ticket(raw, "espocrm", config) → NormalizedTicket
    3. Resolve status / priority / agent / customer / company IDs via SyncService
    4. repo.upsert() — INSERT or UPDATE depending on crm_ticket_id presence
    """
    # ── Safety: check if ticket already exists (even if soft-deleted) ────────
    crm_ticket_id_check = str(raw.get("id", "")).strip() or None
    if crm_ticket_id_check:
        repo_check = TicketRepository(sync.db)
        existing_check = await repo_check.get_by_crm_id(
            crm_ticket_id_check,
            source_system_id,
            tenant_id=tenant_id,
            include_deleted=True,
        )
        if existing_check and existing_check.is_deleted:
            logger.warning(
                "espocrm: Case.create | crm_ticket_id=%s is soft-deleted — "
                "skipping to prevent resurrection",
                crm_ticket_id_check,
            )
            return

    # ── Step 1 & 2: normalize ─────────────────────────────────────────────
    try:
        config = _get_adapter_config("espocrm")
        normalized = normalize_ticket(raw, "espocrm", config)
    except WebhookAdapterError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to normalize EspoCRM ticket id={raw.get('id')!r}: {exc}"
        ) from exc

    # ── Step 3: resolve FK IDs ────────────────────────────────────────────
    try:
        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            raise WebhookSyncError(
                f"Cannot resolve status={normalized.status!r} "
                f"for crm_ticket_id={normalized.crm_ticket_id!r}"
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
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to resolve FK IDs for EspoCRM ticket "
            f"{normalized.crm_ticket_id!r}: {exc}"
        ) from exc

    # ── Step 4: upsert ────────────────────────────────────────────────────
    try:
        repo = TicketRepository(sync.db)
        _, created = await retry_on_conflict(
            repo.upsert,
            crm_ticket_id=normalized.crm_ticket_id,
            source_system_id=source_system_id,
            tenant_id=tenant_id,
            data={
                "tenant_id":         tenant_id,
                "title":             normalized.title,
                "description":       normalized.description,
                "status_id":         status_id,
                "priority_id":       priority_id,
                "agent_id":          agent_id,
                "customer_id":       customer_id,
                "company_id":        company_id,
                "created_at":        normalized.created_at,
                "updated_at":        normalized.updated_at,
                "closed_at":         normalized.closed_at,
                "is_deleted":        False,
            },
        )
        logger.info(
            "espocrm: Case.create | crm_ticket_id=%s | result=%s",
            normalized.crm_ticket_id,
            "created" if created else "updated (upsert hit existing)",
        )
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to persist EspoCRM ticket {normalized.crm_ticket_id!r}: {exc}"
        ) from exc


async def _espo_update_ticket(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """
    EspoCRM Case.update — apply partial updates or create if ticket doesn't exist.
    
    Safety: if ticket is soft-deleted, skip update to prevent resurrection.
    """
    repo = TicketRepository(sync.db)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id, include_deleted=True
    )
    
    if not existing:
        logger.info(
            "espocrm: Case.update | crm_ticket_id=%s not in DB — creating from webhook payload",
            crm_ticket_id,
        )
        await _espo_create_ticket(raw, source_system_id, tenant_id, sync)
        return
    
    # ── Safety: skip if ticket is soft-deleted ────────────────────────────
    if existing.is_deleted:
        logger.warning(
            "espocrm: Case.update | crm_ticket_id=%s is soft-deleted — "
            "skipping to prevent resurrection",
            crm_ticket_id,
        )
        return

    incoming_ts = _parse_iso_timestamp(raw.get("modifiedAt"))
    if incoming_ts and existing.updated_at and incoming_ts <= existing.updated_at:
        logger.info(
            "espocrm: Case.update | crm_ticket_id=%s is stale — skipping",
            crm_ticket_id,
        )
        return

    updates: dict = {}

    if "name" in raw:
        updates["title"] = (raw["name"] or "").strip() or "No Title"
    if "description" in raw:
        updates["description"] = raw.get("description")
    if "modifiedAt" in raw:
        ts = _parse_iso_timestamp(raw["modifiedAt"])
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

    if "priority" in raw:
        priority_str = str(raw["priority"]).lower().strip()
        updates["priority_id"] = await sync._get_priority_id(priority_str)

    if "assignedUserId" in raw:
        updates["agent_id"] = await sync._get_agent_uuid(
            raw["assignedUserId"] or None, source_system_id, tenant_id
        )

    if not updates:
        logger.info(
            "espocrm: Case.update | crm_ticket_id=%s — no recognised fields changed",
            crm_ticket_id,
        )
        return

    try:
        await repo.update(existing, updates)
        logger.info(
            "espocrm: Case.update | crm_ticket_id=%s | fields=%s",
            crm_ticket_id,
            list(updates.keys()),
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to update EspoCRM ticket {crm_ticket_id!r}: {exc}"
        ) from exc


async def _espo_delete_ticket(
    crm_ticket_id: str,
    source_system_id: int,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """
    EspoCRM Case.delete — soft delete.

    Called from two paths:
      1. Direct: payload.event_type == "Case.delete" (header present or inferred)
      2. Fallback: _espo_attempt_delete_fallback() when event_type == "unknown"
    Both paths are idempotent — calling this on an already-deleted ticket is safe.
    """
    repo = TicketRepository(session)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "espocrm: Case.delete | crm_ticket_id=%s not in DB — nothing to delete",
            crm_ticket_id,
        )
        return

    if existing.is_deleted:
        logger.info(
            "espocrm: Case.delete | crm_ticket_id=%s already deleted — idempotent",
            crm_ticket_id,
        )
        return

    try:
        await repo.soft_delete(
            ticket=existing,
            deleted_by_id=None,
            is_deleted_by_crm=True,
        )
        logger.info(
            "espocrm: Case.delete | crm_ticket_id=%s — soft deleted",
            crm_ticket_id,
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to delete EspoCRM ticket {crm_ticket_id!r}: {exc}"
        ) from exc


async def _espo_create_comment(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    integration_id: uuid.UUID,
) -> None:
    """EspoCRM Note.create — fetch full comment stream via adapter and persist."""
    repo = TicketRepository(session)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "espocrm: Note.create | crm_ticket_id=%s not in DB — skipping",
            crm_ticket_id,
        )
        return

    try:
        adapter = await _create_adapter("espocrm", str(integration_id))
        comments_result = await adapter.fetch_comments(crm_ticket_id)

        from app.repositories.comment_repository import CommentRepository

        comment_repo = CommentRepository(session)
        count = 0
        for comment in comments_result.items:
            await comment_repo.upsert(
                ticket_id=existing.id,
                source_system_id=source_system_id,
                crm_comment_id=comment.id,
                body=comment.body,
                comment_type=comment.comment_type or None,
                author_name=comment.author_name,
                author_email=comment.author_email,
                is_internal=comment.is_internal,
                crm_created_at=comment.created_at,
                crm_updated_at=comment.updated_at,
            )
            count += 1

        logger.info(
            "espocrm: Note.create | crm_ticket_id=%s | persisted %d comment(s)",
            crm_ticket_id,
            count,
        )
        await adapter.close()
    except WebhookAdapterError:
        raise
    except Exception as exc:
        raise WebhookCommentError(
            f"Failed to fetch/persist comments for EspoCRM ticket "
            f"{crm_ticket_id!r}: {exc}"
        ) from exc


# ── Zammad Handler ─────────────────────────────────────────────────────────────


async def _handle_zammad(
    payload: RawWebhookPayload,
    session: AsyncSession,
    webhook_secrets: Dict[str, Any] = None,
    outbound_credentials: Dict[str, Any] = None,
) -> None:
    """
    Zammad webhook handler.

    Supported event types (normalised by ZammadWebhookHandler._extract_event):
      create → upsert ticket
      update → partial update ticket
      delete → soft delete ticket
    """
    sync = SyncService(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    for raw in payload.records:
        # Zammad wraps ticket data under a "ticket" key; fall back to root.
        ticket_raw = raw.get("ticket", raw)
        crm_ticket_id = _extract_record_id(ticket_raw, source="zammad")
        if crm_ticket_id is None:
            continue

        try:
            if payload.event_type == "create":
                await _zammad_create_ticket(
                    ticket_raw, source_system_id, tenant_id, sync
                )
            elif payload.event_type == "update":
                await _zammad_update_ticket(
                    crm_ticket_id, ticket_raw, source_system_id, tenant_id, sync
                )
            elif payload.event_type == "delete":
                await _zammad_delete_ticket(
                    crm_ticket_id, source_system_id, tenant_id, session
                )
            else:
                logger.warning(
                    "zammad: unknown event_type=%s | webhook_uuid=%s",
                    payload.event_type,
                    payload.webhook_uuid,
                )
        except WebhookSyncError as exc:
            logger.error(
                "zammad: sync failed | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid, exc,
            )
        except WebhookAdapterError as exc:
            logger.error(
                "zammad: adapter error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid, exc,
            )
        except Exception as exc:
            logger.exception(
                "zammad: unexpected error | id=%s | event=%s | webhook_uuid=%s",
                crm_ticket_id, payload.event_type, payload.webhook_uuid,
                exc_info=exc,
            )


async def _zammad_create_ticket(
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """
    Zammad create — normalize raw webhook payload and persist via SyncService.

    Safety check: if ticket exists but is soft-deleted, skip to prevent resurrection.

    Zammad sends state as a nested object {"name": "open"} (expand=true)
    or as a plain string/integer otherwise. normalize_ticket() resolves
    both forms via AdapterConfig.status_map from crm_adapters.yaml —
    the same config the ZammadAdapter uses at full-sync time.

    Flow mirrors _espo_create_ticket exactly:
    1. _get_adapter_config("zammad") → AdapterConfig  (from factory._registry)
    2. normalize_ticket(raw, "zammad", config) → NormalizedTicket
    3. Resolve FK IDs via SyncService
    4. repo.upsert()
    """
    # ── Safety: check if ticket already exists (even if soft-deleted) ────────
    crm_ticket_id_check = str(raw.get("id", "")).strip() or None
    if crm_ticket_id_check:
        repo_check = TicketRepository(sync.db)
        existing_check = await repo_check.get_by_crm_id(
            crm_ticket_id_check,
            source_system_id,
            tenant_id=tenant_id,
            include_deleted=True,
        )
        if existing_check and existing_check.is_deleted:
            logger.warning(
                "zammad: create | crm_ticket_id=%s is soft-deleted — "
                "skipping to prevent resurrection",
                crm_ticket_id_check,
            )
            return

    # ── Step 1 & 2: normalize ─────────────────────────────────────────────
    try:
        config = _get_adapter_config("zammad")
        normalized = normalize_ticket(raw, "zammad", config)
    except WebhookAdapterError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to normalize Zammad ticket id={raw.get('id')!r}: {exc}"
        ) from exc

    # ── Step 3: resolve FK IDs ────────────────────────────────────────────
    try:
        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            raise WebhookSyncError(
                f"Cannot resolve status={normalized.status!r} "
                f"for crm_ticket_id={normalized.crm_ticket_id!r}"
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
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to resolve FK IDs for Zammad ticket "
            f"{normalized.crm_ticket_id!r}: {exc}"
        ) from exc

    # ── Step 4: upsert ────────────────────────────────────────────────────
    try:
        repo = TicketRepository(sync.db)
        _, created = await retry_on_conflict(
            repo.upsert,
            crm_ticket_id=normalized.crm_ticket_id,
            source_system_id=source_system_id,
            tenant_id=tenant_id,
            data={
                "tenant_id":         tenant_id,
                "title":             normalized.title,
                "description":       normalized.description,
                "status_id":         status_id,
                "priority_id":       priority_id,
                "agent_id":          agent_id,
                "customer_id":       customer_id,
                "company_id":        company_id,
                "created_at":        normalized.created_at,
                "updated_at":        normalized.updated_at,
                "closed_at":         normalized.closed_at,
                "is_deleted":        False,
            },
        )
        logger.info(
            "zammad: ticket create | crm_ticket_id=%s | result=%s",
            normalized.crm_ticket_id,
            "created" if created else "updated (upsert hit existing)",
        )
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to persist Zammad ticket {normalized.crm_ticket_id!r}: {exc}"
        ) from exc


async def _zammad_update_ticket(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """
    Zammad update — apply partial updates or create if ticket doesn't exist.
    
    Safety: if ticket is soft-deleted, skip update to prevent resurrection.
    """
    repo = TicketRepository(sync.db)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id, include_deleted=True
    )
    
    if not existing:
        logger.info(
            "zammad: update | crm_ticket_id=%s not in DB — creating from webhook payload",
            crm_ticket_id,
        )
        await _zammad_create_ticket(raw, source_system_id, tenant_id, sync)
        return
    
    # ── Safety: skip if ticket is soft-deleted ────────────────────────────
    if existing.is_deleted:
        logger.warning(
            "zammad: update | crm_ticket_id=%s is soft-deleted — "
            "skipping to prevent resurrection",
            crm_ticket_id,
        )
        return

    incoming_ts = _parse_iso_timestamp(raw.get("updated_at"))
    if incoming_ts and existing.updated_at and incoming_ts <= existing.updated_at:
        logger.info(
            "zammad: update | crm_ticket_id=%s is stale — skipping",
            crm_ticket_id,
        )
        return

    updates: dict = {}

    if "title" in raw:
        updates["title"] = (raw["title"] or "").strip() or "No Title"
    if "updated_at" in raw:
        ts = _parse_iso_timestamp(raw["updated_at"])
        if ts:
            updates["updated_at"] = ts

    # Zammad sends state as nested {"name": "open"} (expand=true) or plain string.
    state_raw = raw.get("state")
    if state_raw is not None:
        state_str = (
            str(state_raw.get("name", "")).lower().strip()
            if isinstance(state_raw, dict)
            else str(state_raw).lower().strip()
        )
        if state_str:
            status_id = await sync._get_status_id(state_str)
            if status_id:
                updates["status_id"] = status_id
                if state_str in ("closed", "resolved"):
                    updates["closed_at"] = (
                        updates.get("updated_at") or existing.updated_at
                    )
                elif existing.status_id != status_id:
                    updates["closed_at"] = None

    # Zammad sends priority as nested {"name": "2 normal"} (expand=true) or plain string.
    priority_raw = raw.get("priority")
    if priority_raw is not None:
        priority_str = (
            str(priority_raw.get("name", "")).lower().strip()
            if isinstance(priority_raw, dict)
            else str(priority_raw).lower().strip()
        )
        if priority_str:
            updates["priority_id"] = await sync._get_priority_id(priority_str)

    if not updates:
        logger.info(
            "zammad: update | crm_ticket_id=%s — no recognised fields changed",
            crm_ticket_id,
        )
        return

    try:
        await repo.update(existing, updates)
        logger.info(
            "zammad: update | crm_ticket_id=%s | fields=%s",
            crm_ticket_id,
            list(updates.keys()),
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to update Zammad ticket {crm_ticket_id!r}: {exc}"
        ) from exc


async def _zammad_delete_ticket(
    crm_ticket_id: str,
    source_system_id: int,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """Zammad delete — soft delete."""
    repo = TicketRepository(session)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "zammad: delete | crm_ticket_id=%s not in DB — nothing to delete",
            crm_ticket_id,
        )
        return

    if existing.is_deleted:
        logger.info(
            "zammad: delete | crm_ticket_id=%s already deleted — idempotent",
            crm_ticket_id,
        )
        return

    try:
        await repo.soft_delete(
            ticket=existing,
            deleted_by_id=None,
            is_deleted_by_crm=True,
        )
        logger.info(
            "zammad: delete | crm_ticket_id=%s — soft deleted",
            crm_ticket_id,
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to delete Zammad ticket {crm_ticket_id!r}: {exc}"
        ) from exc


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _extract_record_id(
    raw: dict,
    source: str,
    id_key: str = "id",
) -> Optional[str]:
    """Extract and validate CRM record ID from raw webhook data."""
    crm_id = str(raw.get(id_key, "")).strip()
    if not crm_id:
        logger.error(
            "%s: record missing %r field — skipping | keys=%s",
            source,
            id_key,
            list(raw.keys()),
        )
        return None
    return crm_id


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string to a datetime object."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        logger.debug("Could not parse timestamp: %r", value)
        return None


def _get_adapter_config(source_system: str) -> Any:
    """
    Fetch the AdapterConfig for the given CRM from the global factory registry.

    Why this is correct (cross-referenced with adapter_factory.py)
    --------------------------------------------------------------
    CrmAdapterFactory.__init__ stores the registry as ``self._registry``
    (an AdapterRegistry instance).  Internally, CrmAdapterFactory.create()
    fetches the config with:

        config = self._registry.get_adapter_config(crm_type)   # line in factory

    So the correct external call is:

        factory._registry.get_adapter_config(source_system)

    The previous helper (_get_adapter_registry) tried ``factory._adapter_registry``
    — an attribute that does not exist → AttributeError on every create event.

    The returned AdapterConfig is the same object injected into:
      - BaseCrmAdapter.__init__(config=...)
      - SchemaMapper(config=...)
      - normalize_ticket(raw, source_system, config)

    So status_map, priority_map and field mappings are always consistent
    between full-sync and webhook paths.

    Raises
    ------
    WebhookAdapterError
        If the factory is not initialised or the registry lookup fails.
    """
    try:
        from app.adapter_dependencies.deps import get_adapter_factory_instance

        factory: CrmAdapterFactory = get_adapter_factory_instance()
    except RuntimeError as exc:
        raise WebhookAdapterError(
            f"Adapter factory not initialised — cannot normalize "
            f"{source_system!r} ticket: {exc}"
        ) from exc

    try:
        # factory._registry is AdapterRegistry — confirmed in adapter_factory.py:
        #   self._registry = registry   (line in __init__)
        #   config = self._registry.get_adapter_config(crm_type)  (line in create())
        return factory._registry.get_adapter_config(source_system)
    except Exception as exc:
        raise WebhookAdapterError(
            f"Failed to fetch AdapterConfig for {source_system!r} "
            f"from registry: {exc}"
        ) from exc


async def _create_adapter(source_system: str, integration_id: str) -> Any:
    """
    Create and open an adapter for webhook processing.

    Parameters
    ----------
    source_system:
        CRM name used only for log context (e.g. "espocrm").
    integration_id:
        The CrmIntegration UUID string — passed directly to factory.create()
        which fetches credentials from the DB / Infisical internally.
        Must be the UUID string, NOT a credentials dict.
    """
    try:
        from app.adapter_dependencies.deps import get_adapter_factory_instance

        factory = get_adapter_factory_instance()
        adapter = await factory.create(integration_id)
        await adapter.open()
        return adapter
    except (AdapterFactoryError, AuthenticationError) as exc:
        raise WebhookAdapterError(
            f"Failed to create/authenticate {source_system!r} adapter "
            f"for integration_id={integration_id!r}: {exc}"
        ) from exc
    except Exception as exc:
        raise WebhookAdapterError(
            f"Unexpected error creating {source_system!r} adapter "
            f"for integration_id={integration_id!r}: {exc}"
        ) from exc