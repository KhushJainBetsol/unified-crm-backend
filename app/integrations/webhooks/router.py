"""
app/integrations/webhooks/router.py

POST /webhooks/ingest/{webhook_uuid}

One session opened here, passed all the way to the service.
CrmIntegration stays attached to the session for its full lifetime.
Plain scalar values are extracted into RawWebhookPayload inside the
handler so the service has zero SQLAlchemy dependency.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.database import async_session_maker
from app.credentials.encryption import EncryptedPayload, EncryptionService
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.models import InfisicalSettings
from app.integrations.webhooks.errors import WebhookVerificationError
from app.integrations.webhooks.handlers import get_handler
from app.integrations.webhooks.models import RawWebhookPayload
from app.integrations.webhooks.service import handle_raw_webhook
from app.integrations.webhooks.verifier import WebhookVerifier
from app.models.crm_integration import CrmIntegration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# ── Custom exceptions ──────────────────────────────────────────────────────────


class WebhookAuthError(Exception):
    """
    Raised by a handler's verify() when the webhook signature or secret
    does not match.
    """


class WebhookParseError(Exception):
    """
    Raised by a handler's parse() when the request body cannot be decoded
    into a RawWebhookPayload.
    """


# ── Router ─────────────────────────────────────────────────────────────────────


@router.post(
    "/ingest/{webhook_uuid}", summary="Unified webhook ingest", status_code=200
)
async def ingest(webhook_uuid: str, request: Request) -> JSONResponse:
    """
    Single ingest endpoint for all connected CRMs.
    """
    received_at = datetime.now(timezone.utc)

    body = await request.body()

    parsed_uuid = _parse_webhook_uuid(webhook_uuid)

    async with async_session_maker() as session:
        async with session.begin():

            integration = await _get_integration(session, parsed_uuid)

            if integration is None or not integration.is_active:
                logger.warning(
                    "Webhook rejected | webhook_uuid=%s | reason=%s | from=%s",
                    webhook_uuid,
                    "not_found" if integration is None else "inactive",
                    _client_ip(request),
                )
                raise HTTPException(status_code=400, detail="Invalid webhook identifier")

            system_name = integration.source_system.system_name
            handler = get_handler(system_name)

            if handler is None:
                logger.error(
                    "No handler registered | system=%s | integration_id=%s",
                    system_name,
                    integration.id,
                )
                raise HTTPException(status_code=500, detail="Handler not found")

            # ── Decrypt secrets using correct key (tenant or global) ─────────────
            webhook_secrets = await _decrypt_webhook_secrets(integration)
            outbound_credentials = await _decrypt_outbound_credentials(integration)

            # ── Signature / secret verification ──────────────────────────────────
            try:
                verifier = WebhookVerifier(webhook_secrets)
                await verifier.verify(request, body, integration)
            except WebhookVerificationError as exc:
                logger.warning(
                    "Webhook signature verification failed | system=%s | "
                    "webhook_uuid=%s | integration_id=%s | from=%s | reason=%s",
                    system_name,
                    webhook_uuid,
                    integration.id,
                    _client_ip(request),
                    exc,
                )
                raise HTTPException(status_code=400, detail="Invalid webhook identifier")

            try:
                payload: RawWebhookPayload = await handler.parse(
                    request, body, integration
                )
                payload.webhook_uuid = integration.webhook_uuid
            except Exception as exc:
                logger.error(
                    "Webhook parse failed | system=%s | integration_id=%s | "
                    "from=%s | reason=%s",
                    system_name,
                    integration.id,
                    _client_ip(request),
                    exc,
                )
                raise HTTPException(status_code=400, detail="Malformed webhook payload")

            await handle_raw_webhook(
                payload,
                session,
                webhook_secrets=webhook_secrets,
                outbound_credentials=outbound_credentials,
            )

    logger.info(
        "Webhook accepted | webhook_uuid=%s | source=%s | event=%s | records=%d | from=%s | at=%s",
        integration.webhook_uuid,
        payload.source_system,
        payload.event_type,
        len(payload.records),
        _client_ip(request),
        received_at.isoformat(),
    )

    return JSONResponse(status_code=200, content={"status": "accepted"})


# ── Health check ───────────────────────────────────────────────────────────────


@router.get("/health", summary="Webhook layer health check")
async def health() -> dict:
    return {"status": "ok"}


# ── Private helpers ────────────────────────────────────────────────────────────


def _parse_webhook_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook identifier")


async def _get_integration(
    session: AsyncSession,
    webhook_uuid: uuid.UUID,
) -> CrmIntegration | None:
    result = await session.execute(
        select(CrmIntegration)
        .where(CrmIntegration.webhook_uuid == webhook_uuid)
        .options(joinedload(CrmIntegration.source_system))
    )
    return result.scalars().first()


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ── Key resolution helper ──────────────────────────────────────────────────────


def _resolve_key_for_integration(
    key_manager: InfisicalCredentialManager,
    integration: CrmIntegration,
) -> tuple[str, str]:
    kv = integration.key_version

    # Try per-tenant key first
    raw_key = key_manager.get_tenant_key(str(integration.tenant_id), kv)
    if raw_key is not None:
        return kv, raw_key

    # Fall back to global key
    raw_key = key_manager.get_encryption_key(kv)
    return kv, raw_key

# ── Credential decryption helpers ──────────────────────────────────────────────


async def _decrypt_webhook_secrets(integration: CrmIntegration) -> Dict[str, Any]:
    """
    Decrypt webhook_secrets_enc from CrmIntegration.

    Uses the correct key based on integration.key_version:
      "tenant"  → TENANT_KEY_<tenant_id>
      otherwise → ENCRYPTION_KEY_<key_version>  (global/legacy)

    Returns an empty dict if no secrets are configured or if decryption fails.
    Logs warnings but never raises.
    """
    if not integration.webhook_secrets_enc:
        logger.debug(
            "No webhook_secrets_enc configured for integration_id=%s",
            integration.id,
        )
        return {}

    try:
        infisical_settings = InfisicalSettings.from_env()
        key_manager = InfisicalCredentialManager(infisical_settings)

        version, raw_key = _resolve_key_for_integration(key_manager, integration)

        enc_service = EncryptionService(raw_key=raw_key, key_version=version)
        secrets = enc_service.decrypt_dict_from_db(integration.webhook_secrets_enc)
        logger.debug(
            "Decrypted webhook_secrets for integration_id=%s (key_version=%s)",
            integration.id,
            version,
        )
        return secrets
    except Exception as exc:
        logger.warning(
            "Failed to decrypt webhook_secrets_enc for integration_id=%s: %s",
            integration.id,
            exc,
        )
        return {}


async def _decrypt_outbound_credentials(integration: CrmIntegration) -> Dict[str, Any]:
    """
    Decrypt credential_enc from CrmIntegration.

    Uses the correct key based on integration.key_version:
      "tenant"  → TENANT_KEY_<tenant_id>
      otherwise → ENCRYPTION_KEY_<key_version>  (global/legacy)

    Returns an empty dict if no credentials are configured or if decryption fails.
    Logs warnings but never raises.
    """
    if not integration.credential_enc:
        logger.debug(
            "No credential_enc configured for integration_id=%s",
            integration.id,
        )
        return {}

    try:
        infisical_settings = InfisicalSettings.from_env()
        key_manager = InfisicalCredentialManager(infisical_settings)

        version, raw_key = _resolve_key_for_integration(key_manager, integration)

        enc_service = EncryptionService(raw_key=raw_key, key_version=version)
        credentials = enc_service.decrypt_dict_from_db(integration.credential_enc)
        logger.debug(
            "Decrypted credential_enc for integration_id=%s (key_version=%s)",
            integration.id,
            version,
        )
        return credentials
    except Exception as exc:
        logger.warning(
            "Failed to decrypt credential_enc for integration_id=%s: %s",
            integration.id,
            exc,
        )
        return {}