"""
app/integrations/webhooks/service.py

Refactored webhook processing service using adapter pattern, SyncService,
and comprehensive error handling. All secrets come from CrmIntegration.

Bugs fixed
----------
1. Zammad event_type never matched "create"/"update" — _extract_event now
   normalises Zammad's "ticket_create" / "ticket_update" strings to the
   internal keys used by _handle_zammad().

2. EspoCRM comments — handler now matches both "Note.create" (what EspoCRM
   actually sends) and "Comment.create" for forward-compatibility.

3. _create_adapter received outbound_credentials dict instead of the
   integration_id string, so factory.create("") was always called and
   silently failed. integration_id is now passed explicitly from the payload.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict

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

logger = logging.getLogger(__name__)


# ── Entry point ────────────────────────────────────────────────────────────────


async def handle_raw_webhook(
    payload: RawWebhookPayload,
    session: AsyncSession,
    webhook_secrets: Dict[str, Any] = None,
    outbound_credentials: Dict[str, Any] = None,
) -> None:
    """
    Main webhook handler — processes a verified, parsed webhook payload.

    Parameters
    ----------
    payload:
        RawWebhookPayload with all webhook data.
    session:
        AsyncSession for DB operations.
    webhook_secrets:
        Decrypted webhook verification secrets (optional).
    outbound_credentials:
        Decrypted outbound auth credentials (optional).

    Flow
    ----
    1. Dispatch to CRM-specific handler
    2. For each record in payload: normalize, persist via SyncService
    3. For comment events: fetch full comments via adapter, persist via CommentService
    4. Catch all errors per type; log context; never raise

    Note: This function never raises to caller — all errors logged and handled.
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
    """Routes payload to the appropriate CRM handler using adapter pattern."""
    if payload.source_system == "espocrm":
        await _handle_espo(
            payload, session, webhook_secrets, outbound_credentials
        )
    elif payload.source_system == "zammad":
        await _handle_zammad(
            payload, session, webhook_secrets, outbound_credentials
        )
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

    Supported event types (as sent in X-Webhook-Event header):
      Case.create   → upsert ticket
      Case.update   → partial update ticket
      Case.delete   → soft delete ticket
      Note.create   → fetch + persist comments  (EspoCRM sends "Note.create",
                       NOT "Comment.create" — both accepted for safety)
    """
    sync = SyncService(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    for raw in payload.records:
        crm_ticket_id = _extract_record_id(raw, source="espocrm")
        if crm_ticket_id is None:
            continue

        try:
            if payload.event_type == "Case.create":
                await _espo_create_ticket(
                    raw, source_system_id, tenant_id, sync, outbound_credentials
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
                # FIX 2: EspoCRM sends "Note.create", not "Comment.create".
                # Both accepted so any future rename is handled gracefully.
                await _espo_create_comment(
                    crm_ticket_id,
                    raw,
                    source_system_id,
                    tenant_id,
                    session,
                    integration_id=payload.integration_id,  # FIX 3: from payload
                )
            else:
                logger.warning(
                    "espocrm: unknown event_type=%s | webhook_uuid=%s",
                    payload.event_type,
                    payload.webhook_uuid,
                )
        except WebhookSyncError as exc:
            logger.error(
                "espocrm: sync failed | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc,
            )
        except WebhookAdapterError as exc:
            logger.error(
                "espocrm: adapter error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc,
            )
        except WebhookCommentError as exc:
            logger.error(
                "espocrm: comment error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "espocrm: unexpected error | id=%s | event=%s | webhook_uuid=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc_info=exc,
            )


async def _espo_create_ticket(
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
    outbound_credentials: Dict[str, Any],
) -> None:
    """EspoCRM Case.create — normalize and persist via SyncService."""
    try:
        registry = _get_adapter_registry()
        config = registry.get_adapter_config("espocrm")
        normalized = normalize_ticket(raw, "espocrm", config)
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to normalize EspoCRM ticket {raw.get('id')}: {exc}"
        ) from exc

    try:
        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            raise WebhookSyncError(
                f"Cannot resolve status={normalized.status!r} for id={normalized.crm_ticket_id}"
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

        repo = TicketRepository(sync.db)
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
            "espocrm: Case.create | id=%s | %s",
            normalized.crm_ticket_id,
            "created" if created else "updated",
        )
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to persist EspoCRM ticket {normalized.crm_ticket_id}: {exc}"
        ) from exc


async def _espo_update_ticket(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """EspoCRM Case.update — apply partial updates."""
    repo = TicketRepository(sync.db)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "espocrm: Case.update | id=%s not in DB — skipping (full sync will create)",
            crm_ticket_id,
        )
        return

    incoming_ts = _parse_iso_timestamp(raw.get("modifiedAt"))
    if incoming_ts and existing.updated_at and incoming_ts <= existing.updated_at:
        logger.info(
            "espocrm: Case.update | id=%s is stale — skipping",
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
            "espocrm: Case.update | id=%s — no changes detected",
            crm_ticket_id,
        )
        return

    try:
        await repo.update(existing, updates)
        logger.info(
            "espocrm: Case.update | id=%s | fields=%s",
            crm_ticket_id,
            list(updates.keys()),
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to update EspoCRM ticket {crm_ticket_id}: {exc}"
        ) from exc


async def _espo_delete_ticket(
    crm_ticket_id: str,
    source_system_id: int,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """EspoCRM Case.delete — soft delete."""
    repo = TicketRepository(session)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "espocrm: Case.delete | id=%s not in DB — nothing to delete",
            crm_ticket_id,
        )
        return

    if existing.is_deleted:
        logger.info(
            "espocrm: Case.delete | id=%s already deleted — idempotent",
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
            "espocrm: Case.delete | id=%s — soft deleted",
            crm_ticket_id,
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to delete EspoCRM ticket {crm_ticket_id}: {exc}"
        ) from exc


async def _espo_create_comment(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    integration_id: uuid.UUID,          # FIX 3: explicit, not buried in credentials dict
) -> None:
    """
    EspoCRM Note.create — fetch full comment stream via adapter and persist.

    Why integration_id and not outbound_credentials?
    The adapter factory needs the integration_id to look up credentials from
    Infisical itself. Passing the raw credentials dict had no integration_id
    key so factory.create("") was always called and silently failed.
    """
    repo = TicketRepository(session)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "espocrm: Note.create | ticket id=%s not in DB — skipping",
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
            "espocrm: Note.create | id=%s | persisted %d comment(s)",
            crm_ticket_id,
            count,
        )

        await adapter.close()
    except WebhookAdapterError:
        raise
    except Exception as exc:
        raise WebhookCommentError(
            f"Failed to fetch/persist comments for EspoCRM ticket {crm_ticket_id}: {exc}"
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
      create  → upsert ticket
      update  → partial update ticket
      delete  → soft delete ticket
    """
    sync = SyncService(session)
    source_system_id = payload.source_system_id
    tenant_id = payload.tenant_id

    for raw in payload.records:
        ticket_raw = raw.get("ticket", raw)
        crm_ticket_id = _extract_record_id(ticket_raw, source="zammad")
        if crm_ticket_id is None:
            continue

        try:
            if payload.event_type == "create":
                await _zammad_create_ticket(
                    ticket_raw, source_system_id, tenant_id, sync, outbound_credentials
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
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc,
            )
        except WebhookAdapterError as exc:
            logger.error(
                "zammad: adapter error | id=%s | event=%s | webhook_uuid=%s | reason=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "zammad: unexpected error | id=%s | event=%s | webhook_uuid=%s",
                crm_ticket_id,
                payload.event_type,
                payload.webhook_uuid,
                exc_info=exc,
            )


async def _zammad_create_ticket(
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
    outbound_credentials: Dict[str, Any],
) -> None:
    """Zammad create — normalize and persist via SyncService."""
    try:
        registry = _get_adapter_registry()
        config = registry.get_adapter_config("zammad")
        normalized = normalize_ticket(raw, "zammad", config)
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to normalize Zammad ticket {raw.get('id')}: {exc}"
        ) from exc

    try:
        status_id = await sync._get_status_id(normalized.status)
        if not status_id:
            raise WebhookSyncError(
                f"Cannot resolve status={normalized.status!r} for id={normalized.crm_ticket_id}"
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

        repo = TicketRepository(sync.db)
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
            "zammad: ticket create | id=%s | %s",
            normalized.crm_ticket_id,
            "created" if created else "updated",
        )
    except WebhookSyncError:
        raise
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to persist Zammad ticket {normalized.crm_ticket_id}: {exc}"
        ) from exc


async def _zammad_update_ticket(
    crm_ticket_id: str,
    raw: dict,
    source_system_id: int,
    tenant_id: uuid.UUID,
    sync: SyncService,
) -> None:
    """Zammad update — apply partial updates."""
    repo = TicketRepository(sync.db)
    existing = await repo.get_by_crm_id(
        crm_ticket_id, source_system_id, tenant_id=tenant_id
    )
    if not existing:
        logger.warning(
            "zammad: update | id=%s not in DB — skipping (full sync will create)",
            crm_ticket_id,
        )
        return

    incoming_ts = _parse_iso_timestamp(raw.get("updated_at"))
    if incoming_ts and existing.updated_at and incoming_ts <= existing.updated_at:
        logger.info(
            "zammad: update | id=%s is stale — skipping",
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

    # Zammad sends state as a nested object {"name": "open"} when expand=true,
    # or as a plain string depending on the webhook payload format.
    state_raw = raw.get("state")
    if state_raw is not None:
        if isinstance(state_raw, dict):
            state_str = str(state_raw.get("name", "")).lower().strip()
        else:
            state_str = str(state_raw).lower().strip()
        if state_str:
            status_id = await sync._get_status_id(state_str)
            if status_id:
                updates["status_id"] = status_id
                if state_str in ("closed", "resolved"):
                    updates["closed_at"] = updates.get("updated_at") or existing.updated_at
                elif existing.status_id != status_id:
                    updates["closed_at"] = None

    # Zammad sends priority as a nested object {"name": "2 normal"} when expand=true.
    priority_raw = raw.get("priority")
    if priority_raw is not None:
        if isinstance(priority_raw, dict):
            priority_str = str(priority_raw.get("name", "")).lower().strip()
        else:
            priority_str = str(priority_raw).lower().strip()
        if priority_str:
            updates["priority_id"] = await sync._get_priority_id(priority_str)

    if not updates:
        logger.info(
            "zammad: update | id=%s — no changes detected",
            crm_ticket_id,
        )
        return

    try:
        await repo.update(existing, updates)
        logger.info(
            "zammad: update | id=%s | fields=%s",
            crm_ticket_id,
            list(updates.keys()),
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to update Zammad ticket {crm_ticket_id}: {exc}"
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
            "zammad: delete | id=%s not in DB — nothing to delete",
            crm_ticket_id,
        )
        return

    if existing.is_deleted:
        logger.info(
            "zammad: delete | id=%s already deleted — idempotent",
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
            "zammad: delete | id=%s — soft deleted",
            crm_ticket_id,
        )
    except Exception as exc:
        raise WebhookSyncError(
            f"Failed to delete Zammad ticket {crm_ticket_id}: {exc}"
        ) from exc


# ── Helper functions ──────────────────────────────────────────────────────────


def _extract_record_id(
    raw: dict[str, Any], source: str, id_key: str = "id"
) -> str | None:
    """Extract and validate CRM record ID from raw webhook data."""
    crm_id = str(raw.get(id_key, "")).strip()
    if not crm_id:
        logger.error(
            "%s: record missing '%s' field — skipping | keys=%s",
            source,
            id_key,
            list(raw.keys()),
        )
        return None
    return crm_id


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse ISO 8601 timestamp string to datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        logger.debug("Could not parse timestamp: %r", value)
        return None


def _get_adapter_registry():
    """Get the global adapter registry (initialized at app startup)."""
    from app.adapter_dependencies.deps import get_adapter_factory_instance

    try:
        factory = get_adapter_factory_instance()
        return factory._adapter_registry
    except RuntimeError as exc:
        raise WebhookAdapterError(f"Adapter registry not initialized: {exc}") from exc


async def _create_adapter(source_system: str, integration_id: str):
    """
    Create and open an adapter for webhook processing.

    Parameters
    ----------
    source_system:
        CRM name for logging (e.g. "espocrm").
    integration_id:
        The CrmIntegration UUID string — passed to the factory so it can
        fetch credentials from Infisical. NOT the credentials dict itself.

    Bug fixed: previously received outbound_credentials dict and called
    outbound_credentials.get("integration_id", "") which always returned ""
    because the credentials dict only contains {"token": "..."}.
    """
    try:
        from app.adapter_dependencies.deps import get_adapter_factory_instance

        factory = get_adapter_factory_instance()
        adapter = await factory.create(integration_id)
        await adapter.open()
        return adapter
    except (AdapterFactoryError, AuthenticationError) as exc:
        raise WebhookAdapterError(
            f"Failed to create/authenticate adapter for {source_system}: {exc}"
        ) from exc
    except Exception as exc:
        raise WebhookAdapterError(
            f"Unexpected error creating adapter for {source_system}: {exc}"
        ) from exc