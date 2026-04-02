"""
app/integrations/webhooks/handlers/zammad.py
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request

from app.integrations.webhooks.base import BaseWebhookHandler
from app.integrations.webhooks.models import RawWebhookPayload
from app.models.crm_integration import CrmIntegration

logger = logging.getLogger(__name__)


class ZammadWebhookHandler(BaseWebhookHandler):

    async def verify(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> None:
        expected = integration.webhook_secret or ""

        if not expected:
            logger.warning(
                "zammad: token verification skipped for integration=%s (no secret)",
                integration.id,
            )
            return

        if request.headers.get("X-Zammad-Token", "") != expected:
            raise HTTPException(status_code=401, detail="Invalid X-Zammad-Token")

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

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Expected a JSON object from Zammad"
            )

        # Copy plain scalar values out of the ORM object here
        return RawWebhookPayload(
            integration_id=integration.id,
            source_system_id=integration.source_system_id,
            source_system=integration.source_system.system_name,
            tenant_id=integration.tenant_id,
            event_type=self._extract_event(payload),
            records=[payload],
            meta={},
        )

    @staticmethod
    def _extract_event(payload: dict) -> str:
        try:
            event = payload.get("event")
            if isinstance(event, str) and event:
                return event
            ticket = payload.get("ticket")
            if isinstance(ticket, dict):
                state = ticket.get("state")
                if isinstance(state, dict):
                    name = state.get("name")
                    if isinstance(name, str) and name:
                        return name
        except Exception:
            pass
        return "unknown"
