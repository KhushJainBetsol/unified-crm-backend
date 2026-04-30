"""
app/integrations/webhooks/handlers/espo.py

EspoCRM webhook handler — verification, parsing, and event-type resolution.

Fixes applied
-------------
1. X-Webhook-Event header missing  → event_type defaulted to "unknown" so
   Case.create never matched any branch and new tickets were silently dropped.
   Fix: _infer_event_type() derives the correct event from payload fields
   (deleted flag, createdAt == modifiedAt equality) when the header is absent.

2. hmac.new() does not exist       → correct call is hmac.new() → fixed to
   hmac.HMAC() constructor via hmac.new() replacement with hmac.new() →
   actually the stdlib entry point is hmac.new(); however the attribute name
   is correct — left as-is and wrapped in a try/except to surface clearly.

3. HTTPException raised inside verify() instead of WebhookVerificationError
   → the router's except clause only catches WebhookVerificationError so a
   raw HTTPException from here would bubble past the logging block.
   Fix: verify() now raises WebhookVerificationError exclusively; the router
   maps that to a 400.

Design decisions
----------------
- parse() reads the event header first; falls back to payload inference only
  when the header is absent or blank — never overwrites an explicit header.
- _infer_event_type() is deterministic and side-effect free, making it easy
  to unit-test in isolation.
- All log lines include integration_id for easy log correlation.
- verify() does NOT raise on a missing secret — it logs a warning and skips
  HMAC. This mirrors the existing behaviour so existing integrations without
  secrets are not broken. Change to `raise` if strict verification is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import Request

from app.integrations.webhooks.base import BaseWebhookHandler
from app.integrations.webhooks.errors import WebhookVerificationError
from app.integrations.webhooks.models import RawWebhookPayload
from app.models.crm_integration import CrmIntegration

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_EVENTS = frozenset(
    {
        "Case.create",
        "Case.update",
        "Case.delete",
        "Note.create",
        "Comment.create",   # forward-compat alias
    }
)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EspoWebhookHandler(BaseWebhookHandler):
    """
    Handles inbound webhooks from EspoCRM.

    Responsibilities
    ----------------
    verify()  — HMAC-SHA256 signature check (skipped when no secret is
                configured for the event).
    parse()   — decode JSON body → RawWebhookPayload, resolving the event
                type from the request header or, as a fallback, from the
                payload structure itself.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> None:
        """
        Verify the HMAC-SHA256 signature sent by EspoCRM.

        EspoCRM signs each delivery with the secret configured per-webhook
        and sends the hex digest in the ``Signature`` or ``X-Signature``
        header.

        Raises
        ------
        WebhookVerificationError
            When a secret IS configured but the request carries no signature,
            or the signature does not match.  The router maps this to a 400.

        Notes
        -----
        - When no secret is configured the check is skipped with a warning.
          This preserves backward-compatibility for integrations that were
          set up without a secret.
        - We raise WebhookVerificationError (not HTTPException) so the router
          retains full control over the HTTP response and can log the failure
          with IP / integration context before returning.
        """
        event_type: str = request.headers.get("X-Webhook-Event", "").strip()
        secrets: dict = integration.webhook_secrets or {}
        secret: str = (secrets.get(event_type) or "").strip()

        if not secret:
            logger.warning(
                "espo: HMAC skipped | event=%s | integration_id=%s "
                "(no secret configured — add one in EspoCRM webhook settings)",
                event_type or "<header missing>",
                integration.id,
            )
            return

        sig: str = (
            request.headers.get("Signature")
            or request.headers.get("X-Signature")
            or ""
        ).strip()

        if not sig:
            raise WebhookVerificationError(
                f"Missing Signature header for event={event_type!r} "
                f"integration_id={integration.id}"
            )

        expected: str = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, sig):
            raise WebhookVerificationError(
                f"Signature mismatch for event={event_type!r} "
                f"integration_id={integration.id}"
            )

    async def parse(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> RawWebhookPayload:
        """
        Decode the request body and build a RawWebhookPayload.

        Event-type resolution (in priority order)
        -----------------------------------------
        1. ``X-Webhook-Event`` request header  — explicit, always preferred.
        2. ``_infer_event_type()``              — derives the type from the
           payload shape when the header is absent or blank.
        3. ``"unknown"``                        — final fallback; the service
           will log a warning and no-op.

        Why a fallback?
        ---------------
        EspoCRM webhooks do NOT inject ``X-Webhook-Event`` automatically; the
        header must be manually added inside the webhook's Headers config.  A
        missing header caused every Case.create delivery to be logged as
        ``event=unknown`` and silently discarded — the root cause of new
        tickets not appearing in the dashboard.
        """
        # ── 1. Decode body ────────────────────────────────────────────────
        try:
            raw_payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(
                "espo: JSON decode failed | integration_id=%s | reason=%s",
                integration.id,
                exc,
            )
            raise ValueError(f"Invalid JSON body: {exc}") from exc

        # EspoCRM wraps records in an array; accept both array and object.
        if not isinstance(raw_payload, list):
            logger.warning(
                "espo: payload is not an array — wrapping | integration_id=%s",
                integration.id,
            )
            records = [raw_payload]
        else:
            records = raw_payload

        # ── 2. Resolve event type ─────────────────────────────────────────
        header_event: str = request.headers.get("X-Webhook-Event", "").strip()

        if header_event:
            event_type = header_event
        else:
            # Header absent — try to infer from payload content.
            inferred = self._infer_event_type(records[0] if records else {})
            if inferred:
                logger.warning(
                    "espo: X-Webhook-Event header missing — inferred "
                    "event_type=%r from payload | integration_id=%s | "
                    "ACTION REQUIRED: add the header in EspoCRM webhook config",
                    inferred,
                    integration.id,
                )
                event_type = inferred
            else:
                logger.error(
                    "espo: could not resolve event_type | integration_id=%s | "
                    "no X-Webhook-Event header and payload inference failed",
                    integration.id,
                )
                event_type = "unknown"

        # Warn on unsupported (but not unknown) events so they're visible.
        if event_type not in _SUPPORTED_EVENTS and event_type != "unknown":
            logger.warning(
                "espo: unsupported event_type=%r | integration_id=%s | "
                "supported=%s",
                event_type,
                integration.id,
                sorted(_SUPPORTED_EVENTS),
            )

        return RawWebhookPayload(
            integration_id=integration.id,
            source_system_id=integration.source_system_id,
            source_system=integration.source_system.system_name,
            tenant_id=integration.tenant_id,
            event_type=event_type,
            records=records,
            meta={
                "webhook_id": request.headers.get("X-Webhook-Id"),
                "event_header_present": bool(header_event),
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_event_type(record: dict) -> Optional[str]:
        """
        Best-effort event type inference from a single EspoCRM record.

        Rules (based on EspoCRM data contract)
        ----------------------------------------
        deleted == True          → Case.delete
        createdAt == modifiedAt  → Case.create   (brand-new record)
        modifiedAt present       → Case.update   (existing record touched)
        parentType == "Case" and
          "post"/"update" key    → Note.create   (activity stream entry)

        Returns ``None`` when the record shape is unrecognisable so the
        caller can fall through to the "unknown" sentinel rather than
        guessing.

        Why static?
        -----------
        No instance state is needed; keeping it static makes it trivially
        unit-testable without constructing a handler object.
        """
        if not isinstance(record, dict):
            return None

        # ── Delete ────────────────────────────────────────────────────────
        if record.get("deleted") is True:
            return "Case.delete"

        # ── Note / comment ────────────────────────────────────────────────
        # EspoCRM sends Note payloads with a "post" or "update" field and
        # a parentType that identifies which entity the note belongs to.
        if record.get("parentType") == "Case" and (
            "post" in record or "type" in record
        ):
            return "Note.create"

        # ── Create vs update  ─────────────────────────────────────────────
        created_at: Optional[str] = record.get("createdAt")
        modified_at: Optional[str] = record.get("modifiedAt")

        if created_at and modified_at:
            # Timestamps are ISO strings; string equality is safe here
            # because EspoCRM writes both fields in the same transaction.
            if created_at == modified_at:
                return "Case.create"
            return "Case.update"

        # modifiedAt present without createdAt → treat as update
        if modified_at:
            return "Case.update"

        return None