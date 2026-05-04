"""
app/integrations/webhooks/handlers/zammad.py

Bug fixed
---------
_extract_event previously returned the raw Zammad event string
(e.g. "ticket_create", "ticket_update") or fell back to the ticket
state name (e.g. "new", "open"). Neither matched the internal keys
("create", "update", "delete") used by _handle_zammad() in service.py,
so every Zammad webhook hit the `else` branch and was silently discarded.

Fix: normalise the raw event string to the internal key before returning.
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
        """
        Zammad sends its shared secret in the X-Zammad-Token header.
        Verification is skipped (with a warning) when no secret is configured
        so that integrations without HMAC still receive webhooks.
        """
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

        # --- Normalize Zammad ticket fields using config ---
        from app.integrations.normalizer.registry import _get_adapter_config
        if integration.source_system.system_name.lower() == "zammad":
            config = _get_adapter_config("zammad")
            payload = {
                **payload,
                "ticket": normalize_zammad_ticket_fields(payload.get("ticket", payload), config)
            }

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
        """
        Normalise the Zammad event to our internal keys: "create" | "update" | "delete".

        Zammad sends an "event" field like:
          "ticket_create"  →  "create"
          "ticket_update"  →  "update"
          "ticket_delete"  →  "delete"  (if ever added)

        Falls back to inspecting the ticket state name if the event field is
        absent, which should not happen in practice but guards against
        unexpected payload shapes.
        """
        try:
            event = payload.get("event", "")
            if isinstance(event, str) and event:
                event_lower = event.lower()
                if "create" in event_lower:
                    return "create"
                if "update" in event_lower:
                    return "update"
                if "delete" in event_lower:
                    return "delete"

            # Fallback — inspect ticket state (does NOT determine create vs update,
            # but at least avoids "unknown" for payloads missing the event field)
            ticket = payload.get("ticket")
            if isinstance(ticket, dict):
                state = ticket.get("state")
                if isinstance(state, dict):
                    state_name = state.get("name", "")
                elif isinstance(state, str):
                    state_name = state
                else:
                    state_name = ""

                if state_name:
                    logger.warning(
                        "zammad: 'event' field missing — inferred from ticket state '%s'. "
                        "Treating as 'update'.",
                        state_name,
                    )
                    return "update"

        except Exception as exc:
            logger.warning("zammad: _extract_event failed: %s", exc)

        logger.warning(
            "zammad: could not determine event type from payload keys=%s",
            list(payload.keys()),
        )
        return "unknown"

# --- Zammad normalization utility ---
def normalize_zammad_ticket_fields(payload: dict, config) -> dict:
    """
    Normalizes Zammad ticket payload fields using config.yaml mapping.
    Handles integer state_id and priority_id, mapping them to canonical names.
    Returns a new dict with normalized fields.
    """
    ticket = payload.get("ticket", payload)
    # Defensive: handle both root-level and nested ticket
    normalized = dict(ticket)

    # Normalize state/state_id
    state_id = ticket.get("state_id")
    if state_id is not None:
        state_key = str(state_id)
        mapped_state = config.status_map.get(state_key)
        if mapped_state:
            normalized["state"] = mapped_state
    # If state is a string, map it as well
    elif "state" in ticket:
        state_val = ticket["state"]
        mapped_state = config.status_map.get(str(state_val).lower())
        if mapped_state:
            normalized["state"] = mapped_state

    # Normalize priority_id
    priority_id = ticket.get("priority_id")
    if priority_id is not None:
        priority_key = str(priority_id)
        mapped_priority = config.priority_map.get(priority_key)
        if mapped_priority:
            normalized["priority"] = mapped_priority
    # If priority is a string, map it as well
    elif "priority" in ticket:
        priority_val = ticket["priority"]
        mapped_priority = config.priority_map.get(str(priority_val).lower())
        if mapped_priority:
            normalized["priority"] = mapped_priority

    return normalized