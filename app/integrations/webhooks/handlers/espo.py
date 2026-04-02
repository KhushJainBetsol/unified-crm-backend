"""
app/integrations/webhooks/handlers/espo.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import HTTPException, Request

from app.integrations.webhooks.base import BaseWebhookHandler
from app.integrations.webhooks.models import RawWebhookPayload
from app.models.crm_integration import CrmIntegration

logger = logging.getLogger(__name__)


class EspoWebhookHandler(BaseWebhookHandler):

    async def verify(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> None:
        event_type = request.headers.get("X-Webhook-Event", "")
        secrets: dict = integration.webhook_secrets or {}
        secret: str = secrets.get(event_type, "") or ""

        if not secret:
            logger.warning(
                "espo: HMAC skipped for event=%s integration=%s (no secret configured)",
                event_type,
                integration.id,
            )
            return

        sig = request.headers.get("Signature") or request.headers.get("X-Signature")
        if not sig:
            raise HTTPException(status_code=401, detail="Missing Signature header")

        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(status_code=401, detail="Signature mismatch")

    async def parse(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> RawWebhookPayload:
        try:
            payload = json.loads(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

        if not isinstance(payload, list):
            logger.warning("espo: payload is not an array — wrapping.")
            payload = [payload]

        event_type = request.headers.get("X-Webhook-Event", "unknown")

        # Copy plain scalar values out of the ORM object here
        # so RawWebhookPayload has no SQLAlchemy session dependency
        return RawWebhookPayload(
            integration_id=integration.id,
            source_system_id=integration.source_system_id,
            source_system=integration.source_system.system_name,
            tenant_id=integration.tenant_id,
            event_type=event_type,
            records=payload,
            meta={"webhook_id": request.headers.get("X-Webhook-Id")},
        )
