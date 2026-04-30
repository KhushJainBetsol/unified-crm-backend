# """
# app/integrations/webhooks/router.py

# POST /webhooks/ingest/{webhook_uuid}

# One session opened here, passed all the way to the service.
# CrmIntegration stays attached to the session for its full lifetime.
# Plain scalar values are extracted into RawWebhookPayload inside the
# handler so the service has zero SQLAlchemy dependency.
# """

# from __future__ import annotations

# import json
# import logging
# import uuid
# from datetime import datetime, timezone
# from typing import Any, Dict

# from fastapi import APIRouter, HTTPException, Request
# from fastapi.responses import JSONResponse
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy.orm import joinedload

# from app.core.database import async_session_maker
# from app.credentials.encryption import EncryptedPayload, EncryptionService
# from app.credentials.manager import InfisicalCredentialManager
# from app.credentials.models import InfisicalSettings
# from app.integrations.webhooks.errors import WebhookVerificationError
# from app.integrations.webhooks.handlers import get_handler
# from app.integrations.webhooks.models import RawWebhookPayload
# from app.integrations.webhooks.service import handle_raw_webhook
# from app.integrations.webhooks.verifier import WebhookVerifier
# from app.models.crm_integration import CrmIntegration

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# # ── Custom exceptions ──────────────────────────────────────────────────────────


# class WebhookAuthError(Exception):
#     """
#     Raised by a handler's verify() when the webhook signature or secret
#     does not match.

#     Why a dedicated exception instead of raising HTTPException directly
#     inside verify()?
#     Keeping HTTP concerns out of the handler layer makes handlers easier to
#     unit-test (no need to mock FastAPI internals) and gives the router full
#     control over the response — e.g. logging the client IP, returning a
#     generic 400 instead of a 401 to avoid leaking that a valid endpoint exists.
#     """


# class WebhookParseError(Exception):
#     """
#     Raised by a handler's parse() when the request body cannot be decoded
#     into a RawWebhookPayload.

#     Use case: the CRM sent a malformed JSON body, an unexpected Content-Type,
#     or a payload shape that the handler does not recognise. We return 400 so
#     the CRM knows not to retry this exact payload.
#     """


# # ── Router ─────────────────────────────────────────────────────────────────────


# @router.post(
#     "/ingest/{webhook_uuid}", summary="Unified webhook ingest", status_code=200
# )
# async def ingest(webhook_uuid: str, request: Request) -> JSONResponse:
#     """
#     Single ingest endpoint for all connected CRMs.

#     Design decisions:
#     - Body is read before any await on `request` to avoid stream exhaustion.
#     - UUID is validated before touching the DB to avoid unnecessary queries.
#     - Credentials are decrypted once and passed to service.
#     - All HTTPExceptions use generic messages to avoid leaking integration
#       details to an unauthenticated caller.
#     - handle_raw_webhook never raises; errors are logged inside the service.
#       This guarantees a 200 ACK so the CRM does not retry a valid delivery.
#     """
#     received_at = datetime.now(timezone.utc)

#     # Read body before any other await on the request.
#     # Why: FastAPI's Request body stream can only be consumed once. Reading it
#     # here ensures verify() and parse() both receive the raw bytes regardless
#     # of their internal implementation.
#     body = await request.body()

#     parsed_uuid = _parse_webhook_uuid(webhook_uuid)

#     async with async_session_maker() as session:
#         async with session.begin():

#             integration = await _get_integration(session, parsed_uuid)

#             # Return the same 400 whether the UUID is unknown or the
#             # integration is inactive. Differentiating the two would tell an
#             # attacker which UUIDs are valid.
#             if integration is None or not integration.is_active:
#                 logger.warning(
#                     "Webhook rejected | webhook_uuid=%s | reason=%s | from=%s",
#                     webhook_uuid,
#                     "not_found" if integration is None else "inactive",
#                     _client_ip(request),
#                 )
#                 raise HTTPException(status_code=400, detail="Invalid webhook identifier")

#             system_name = integration.source_system.system_name
#             handler = get_handler(system_name)

#             if handler is None:
#                 # This is a server-side misconfiguration, not a caller error.
#                 logger.error(
#                     "No handler registered | system=%s | integration_id=%s",
#                     system_name,
#                     integration.id,
#                 )
#                 raise HTTPException(status_code=500, detail="Handler not found")

#             # ── Decrypt secrets for verification and processing ──────────────────────
#             webhook_secrets = await _decrypt_webhook_secrets(integration)
#             outbound_credentials = await _decrypt_outbound_credentials(integration)

#             # ── Signature / secret verification using centralized verifier ─────────
#             try:
#                 verifier = WebhookVerifier(webhook_secrets)
#                 await verifier.verify(request, body, integration)
#             except WebhookVerificationError as exc:
#                 logger.warning(
#                     "Webhook signature verification failed | system=%s | "
#                     "webhook_uuid=%s | integration_id=%s | from=%s | reason=%s",
#                     system_name,
#                     webhook_uuid,
#                     integration.id,
#                     _client_ip(request),
#                     exc,
#                 )
#                 raise HTTPException(status_code=400, detail="Invalid webhook identifier")

#             # Parse raw bytes → RawWebhookPayload.
#             # 400 on parse failure tells the CRM the payload itself is bad;
#             # it should not retry without fixing the payload.
#             try:
#                 payload: RawWebhookPayload = await handler.parse(
#                     request, body, integration
#                 )
#                 # Inject webhook_uuid into payload for tracing
#                 payload.webhook_uuid = integration.webhook_uuid
#             except Exception as exc:
#                 logger.error(
#                     "Webhook parse failed | system=%s | integration_id=%s | "
#                     "from=%s | reason=%s",
#                     system_name,
#                     integration.id,
#                     _client_ip(request),
#                     exc,
#                 )
#                 raise HTTPException(status_code=400, detail="Malformed webhook payload")

#             # Business logic — never raises; all errors are logged inside.
#             # Pass both webhook secrets and outbound credentials for full processing
#             await handle_raw_webhook(
#                 payload,
#                 session,
#                 webhook_secrets=webhook_secrets,
#                 outbound_credentials=outbound_credentials,
#             )

#     logger.info(
#         "Webhook accepted | webhook_uuid=%s | source=%s | event=%s | records=%d | from=%s | at=%s",
#         integration.webhook_uuid,
#         payload.source_system,
#         payload.event_type,
#         len(payload.records),
#         _client_ip(request),
#         received_at.isoformat(),
#     )

#     return JSONResponse(status_code=200, content={"status": "accepted"})


# # ── Health check ───────────────────────────────────────────────────────────────


# @router.get("/health", summary="Webhook layer health check")
# async def health() -> dict:
#     """
#     Lightweight liveness probe.
#     Does not check DB connectivity — that belongs in a dedicated /health
#     endpoint at the application level, not per-router.
#     """
#     return {"status": "ok"}


# # ── Private helpers ────────────────────────────────────────────────────────────


# def _parse_webhook_uuid(raw: str) -> uuid.UUID:
#     """
#     Validates and parses the webhook UUID from the URL path segment.

#     Why raise HTTPException here instead of in the route?
#     Keeping validation close to its data source makes the route body
#     easier to read. A 400 with a clear detail is correct — the caller
#     provided a syntactically invalid identifier.
#     """
#     try:
#         return uuid.UUID(raw)
#     except ValueError:
#         raise HTTPException(status_code=400, detail="Invalid webhook identifier")


# async def _get_integration(
#     session: AsyncSession,
#     webhook_uuid: uuid.UUID,
# ) -> CrmIntegration | None:
#     """
#     Fetches the CrmIntegration by webhook UUID, eagerly loading source_system.

#     Why joinedload?
#     source_system.system_name is accessed immediately after this call.
#     joinedload avoids a second round-trip to the DB and prevents the
#     'DetachedInstanceError' that would occur if source_system were loaded
#     lazily outside the session context.
#     """
#     result = await session.execute(
#         select(CrmIntegration)
#         .where(CrmIntegration.webhook_uuid == webhook_uuid)
#         .options(joinedload(CrmIntegration.source_system))
#     )
#     return result.scalars().first()


# def _client_ip(request: Request) -> str:
#     """
#     Extracts the client IP for logging, handling the case where
#     the app sits behind a reverse proxy (X-Forwarded-For header).

#     Why not use request.client.host directly everywhere?
#     Behind a proxy (nginx, AWS ALB, Cloudflare), request.client.host
#     is always the proxy's IP. X-Forwarded-For carries the real client IP.
#     Centralising this logic ensures all log lines are consistent.

#     Why only take the first value?
#     X-Forwarded-For can be a comma-separated chain when multiple proxies
#     are involved. The first entry is always the original client.
#     """
#     forwarded_for = request.headers.get("X-Forwarded-For")
#     if forwarded_for:
#         return forwarded_for.split(",")[0].strip()
#     if request.client:
#         return request.client.host
#     return "unknown"


# # ── Credential decryption helpers ──────────────────────────────────────────────


# async def _decrypt_webhook_secrets(integration: CrmIntegration) -> Dict[str, Any]:
#     """
#     Decrypt webhook_secrets_enc from CrmIntegration.

#     Returns an empty dict if no secrets are configured or if decryption fails.
#     Logs warnings but never raises — webhook can still proceed with limited
#     verification capability.
#     """
#     if not integration.webhook_secrets_enc:
#         logger.debug(
#             "No webhook_secrets_enc configured for integration_id=%s",
#             integration.id,
#         )
#         return {}

#     try:
#         infisical_settings = InfisicalSettings.from_env()
#         key_manager = InfisicalCredentialManager(infisical_settings)
#         version, raw_key = key_manager.get_active_key_and_version()

#         enc_service = EncryptionService(raw_key=raw_key, key_version=version)
#         secrets = enc_service.decrypt_dict_from_db(integration.webhook_secrets_enc)
#         logger.debug(
#             "Successfully decrypted webhook_secrets for integration_id=%s",
#             integration.id,
#         )
#         return secrets
#     except Exception as exc:
#         logger.warning(
#             "Failed to decrypt webhook_secrets_enc for integration_id=%s: %s",
#             integration.id,
#             exc,
#         )
#         return {}


# async def _decrypt_outbound_credentials(integration: CrmIntegration) -> Dict[str, Any]:
#     """
#     Decrypt credential_enc from CrmIntegration.

#     Returns an empty dict if no credentials are configured or if decryption fails.
#     Logs warnings but never raises — webhook processing continues but adapter
#     authentication may fail at that point.
#     """
#     if not integration.credential_enc:
#         logger.debug(
#             "No credential_enc configured for integration_id=%s",
#             integration.id,
#         )
#         return {}

#     try:
#         infisical_settings = InfisicalSettings.from_env()
#         key_manager = InfisicalCredentialManager(infisical_settings)
#         version, raw_key = key_manager.get_active_key_and_version()

#         enc_service = EncryptionService(raw_key=raw_key, key_version=version)
#         credentials = enc_service.decrypt_dict_from_db(integration.credential_enc)
#         logger.debug(
#             "Successfully decrypted credential_enc for integration_id=%s",
#             integration.id,
#         )
#         return credentials
#     except Exception as exc:
#         logger.warning(
#             "Failed to decrypt credential_enc for integration_id=%s: %s",
#             integration.id,
#             exc,
#         )
#         return {}

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
    """
    Fetch the correct AES key for a CrmIntegration row.

    key_version == "tenant"  → TENANT_KEY_<tenant_id>   (per-tenant key)
    anything else            → ENCRYPTION_KEY_<version>  (global/legacy key)

    Returns
    -------
    (version, raw_key)
        version  : the key_version string stored on the row (used to build
                   EncryptionService so it embeds the right version in
                   EncryptedPayload)
        raw_key  : raw AES key string for EncryptionService

    Raises
    ------
    ValueError
        If key_version == "tenant" but no TENANT_KEY_<tenant_id> exists in
        Infisical (tenant was created before the per-tenant key rollout or
        key generation failed at tenant-creation time).
    """
    if integration.key_version == "tenant":
        raw_key = key_manager.get_tenant_key(str(integration.tenant_id))
        if raw_key is None:
            raise ValueError(
                f"Per-tenant key TENANT_KEY_{integration.tenant_id} not found "
                "in Infisical. Ensure the tenant was created via "
                "POST /super-admin/tenants after the per-tenant key rollout, "
                "or manually add the secret."
            )
        return "tenant", raw_key

    # Global / legacy key path — version already stored on the row
    raw_key = key_manager.get_encryption_key(integration.key_version)
    return integration.key_version, raw_key


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