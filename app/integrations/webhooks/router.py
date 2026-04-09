"""
app/integrations/webhooks/router.py

POST /webhooks/ingest/{webhook_uuid}

One session opened here, passed all the way to the service.
CrmIntegration stays attached to the session for its full lifetime.
Plain scalar values are extracted into RawWebhookPayload inside the
handler so the service has zero SQLAlchemy dependency.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.database import async_session_maker
from app.integrations.webhooks.handlers import get_handler
from app.integrations.webhooks.models import RawWebhookPayload
from app.integrations.webhooks.service import handle_raw_webhook
from app.models.crm_integration import CrmIntegration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# ── Custom exceptions ──────────────────────────────────────────────────────────


class WebhookAuthError(Exception):
    """
    Raised by a handler's verify() when the webhook signature or secret
    does not match.

    Why a dedicated exception instead of raising HTTPException directly
    inside verify()?
    Keeping HTTP concerns out of the handler layer makes handlers easier to
    unit-test (no need to mock FastAPI internals) and gives the router full
    control over the response — e.g. logging the client IP, returning a
    generic 400 instead of a 401 to avoid leaking that a valid endpoint exists.
    """


class WebhookParseError(Exception):
    """
    Raised by a handler's parse() when the request body cannot be decoded
    into a RawWebhookPayload.

    Use case: the CRM sent a malformed JSON body, an unexpected Content-Type,
    or a payload shape that the handler does not recognise. We return 400 so
    the CRM knows not to retry this exact payload.
    """


# ── Router ─────────────────────────────────────────────────────────────────────


@router.post(
    "/ingest/{webhook_uuid}", summary="Unified webhook ingest", status_code=200
)
async def ingest(webhook_uuid: str, request: Request) -> JSONResponse:
    """
    Single ingest endpoint for all connected CRMs.

    Design decisions:
    - Body is read before any await on `request` to avoid stream exhaustion.
    - UUID is validated before touching the DB to avoid unnecessary queries.
    - All HTTPExceptions use generic messages to avoid leaking integration
      details to an unauthenticated caller (e.g. distinguishing "not found"
      from "inactive" would reveal that a UUID is valid).
    - handle_raw_webhook never raises; errors are logged inside the service.
      This guarantees a 200 ACK so the CRM does not retry a valid delivery.
    """
    received_at = datetime.now(timezone.utc)

    # Read body before any other await on the request.
    # Why: FastAPI's Request body stream can only be consumed once. Reading it
    # here ensures verify() and parse() both receive the raw bytes regardless
    # of their internal implementation.
    body = await request.body()

    parsed_uuid = _parse_webhook_uuid(webhook_uuid)

    async with async_session_maker() as session:
        async with session.begin():

            integration = await _get_integration(session, parsed_uuid)

            # Return the same 400 whether the UUID is unknown or the
            # integration is inactive. Differentiating the two would tell an
            # attacker which UUIDs are valid.
            if integration is None or not integration.is_active:
                logger.warning(
                    "Webhook rejected | uuid=%s | reason=%s | from=%s",
                    webhook_uuid,
                    "not_found" if integration is None else "inactive",
                    _client_ip(request),
                )
                raise HTTPException(status_code=400, detail="Invalid webhook identifier")

            system_name = integration.source_system.system_name
            handler = get_handler(system_name)

            if handler is None:
                # This is a server-side misconfiguration, not a caller error.
                logger.error(
                    "No handler registered | system=%s | integration_id=%s",
                    system_name,
                    integration.id,
                )
                raise HTTPException(status_code=500, detail="Handler not found")

            # Signature / secret verification.
            # A failed verify should return 400, not 401/403, to avoid
            # confirming that the endpoint exists and accepts that CRM's format.
            try:
                await handler.verify(request, body, integration)
            except WebhookAuthError as exc:
                logger.warning(
                    "Webhook signature verification failed | system=%s | "
                    "integration_id=%s | from=%s | reason=%s",
                    system_name,
                    integration.id,
                    _client_ip(request),
                    exc,
                )
                raise HTTPException(status_code=400, detail="Invalid webhook identifier")

            # Parse raw bytes → RawWebhookPayload.
            # 400 on parse failure tells the CRM the payload itself is bad;
            # it should not retry without fixing the payload.
            try:
                payload: RawWebhookPayload = await handler.parse(
                    request, body, integration
                )
            except WebhookParseError as exc:
                logger.error(
                    "Webhook parse failed | system=%s | integration_id=%s | "
                    "from=%s | reason=%s",
                    system_name,
                    integration.id,
                    _client_ip(request),
                    exc,
                )
                raise HTTPException(status_code=400, detail="Malformed webhook payload")

            # Business logic — never raises; all errors are logged inside.
            await handle_raw_webhook(payload, session)

    logger.info(
        "Webhook accepted | source=%s | event=%s | records=%d | from=%s | at=%s",
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
    """
    Lightweight liveness probe.
    Does not check DB connectivity — that belongs in a dedicated /health
    endpoint at the application level, not per-router.
    """
    return {"status": "ok"}


# ── Private helpers ────────────────────────────────────────────────────────────


def _parse_webhook_uuid(raw: str) -> uuid.UUID:
    """
    Validates and parses the webhook UUID from the URL path segment.

    Why raise HTTPException here instead of in the route?
    Keeping validation close to its data source makes the route body
    easier to read. A 400 with a clear detail is correct — the caller
    provided a syntactically invalid identifier.
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook identifier")


async def _get_integration(
    session: AsyncSession,
    webhook_uuid: uuid.UUID,
) -> CrmIntegration | None:
    """
    Fetches the CrmIntegration by webhook UUID, eagerly loading source_system.

    Why joinedload?
    source_system.system_name is accessed immediately after this call.
    joinedload avoids a second round-trip to the DB and prevents the
    'DetachedInstanceError' that would occur if source_system were loaded
    lazily outside the session context.
    """
    result = await session.execute(
        select(CrmIntegration)
        .where(CrmIntegration.webhook_uuid == webhook_uuid)
        .options(joinedload(CrmIntegration.source_system))
    )
    return result.scalars().first()


def _client_ip(request: Request) -> str:
    """
    Extracts the client IP for logging, handling the case where
    the app sits behind a reverse proxy (X-Forwarded-For header).

    Why not use request.client.host directly everywhere?
    Behind a proxy (nginx, AWS ALB, Cloudflare), request.client.host
    is always the proxy's IP. X-Forwarded-For carries the real client IP.
    Centralising this logic ensures all log lines are consistent.

    Why only take the first value?
    X-Forwarded-For can be a comma-separated chain when multiple proxies
    are involved. The first entry is always the original client.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"