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


@router.post(
    "/ingest/{webhook_uuid}", summary="Unified webhook ingest", status_code=200
)
async def ingest(webhook_uuid: str, request: Request) -> JSONResponse:
    received_at = datetime.now(timezone.utc)

    # Read body before any other await on the request
    body = await request.body()

    # Validate UUID format before touching DB
    try:
        parsed_uuid = uuid.UUID(webhook_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook identifier")

    async with async_session_maker() as session:
        async with session.begin():

            # 1. Resolve integration — joinedload keeps source_system attached
            integration = await _get_integration(session, parsed_uuid)
            if integration is None:
                raise HTTPException(
                    status_code=400, detail="Invalid webhook identifier"
                )

            # 2. Check active
            if not integration.is_active:
                raise HTTPException(status_code=400, detail="Integration is inactive")

            # 3. Pick handler by CRM type
            system_name = integration.source_system.system_name
            handler = get_handler(system_name)
            if handler is None:
                logger.error(
                    "No handler for system_name=%s integration=%s",
                    system_name,
                    integration.id,
                )
                raise HTTPException(status_code=500, detail="Handler not found")

            # 4. Verify — reads secrets from integration directly
            await handler.verify(request, body, integration)

            # 5. Parse — copies plain scalars into RawWebhookPayload,
            #    no ORM object reference kept after this point
            payload: RawWebhookPayload = await handler.parse(request, body, integration)

            # 6. Process — service receives session + plain dataclass, no ORM dependency
            await handle_raw_webhook(payload, session)

    logger.info(
        "Webhook accepted | source=%s | event=%s | records=%d | from=%s | at=%s",
        payload.source_system,
        payload.event_type,
        len(payload.records),
        request.client.host if request.client else "unknown",
        received_at.isoformat(),
    )

    return JSONResponse(status_code=200, content={"status": "accepted"})


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


@router.get("/health", summary="Webhook layer health check")
async def health() -> dict:
    return {"status": "ok"}
