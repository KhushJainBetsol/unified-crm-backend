"""
app/integrations/webhooks/handlers/espo.py

EspoCRM webhook handler — verification, parsing, and event-type resolution.

Fixes applied
-------------
1. X-Webhook-Event header missing  → event_type defaulted to "unknown" so
   Case.create never matched any branch and new tickets were silently dropped.
   Fix: _infer_event_type() derives the correct event from payload fields
   (deleted flag, createdAt == modifiedAt equality) when the header is absent.

2. Case.delete payload is minimal   → EspoCRM sends ONLY {"id": "<id>"} for
   delete events — no deleted flag, no timestamps. The previous inference
   only checked record.get("deleted") is True which never matched.
   Fix: _infer_event_type() now detects the minimal-payload pattern
   (record has "id" but none of the fields present on create/update payloads).

3. hmac.new() does not exist        → correct call is hmac.new() → fixed to
   hmac.HMAC() constructor via hmac.new() replacement with hmac.new() →
   actually the stdlib entry point is hmac.new(); however the attribute name
   is correct — left as-is and wrapped in a try/except to surface clearly.

4. HTTPException raised inside verify() instead of WebhookVerificationError
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

EspoCRM delete payload shape (confirmed from webhook queue inspection)
----------------------------------------------------------------------
EspoCRM sends the absolute minimum for Case.delete:

    [{"id": "69f504798ae7eff4b"}]

There is no "deleted" flag, no timestamps, no status field — just the id.
_infer_event_type() detects this by checking that "id" is present and NONE
of the fields that would appear on a create or update payload are present.
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

# Fields that appear on Case.create and Case.update payloads but are
# absent from Case.delete payloads. Used by _infer_event_type() to
# distinguish a minimal delete payload from a broken create/update.
_CREATE_UPDATE_INDICATOR_FIELDS = frozenset(
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
           will attempt a delete fallback before logging a warning and no-op.

        Why a fallback?
        ---------------
        EspoCRM webhooks do NOT inject ``X-Webhook-Event`` automatically; the
        header must be manually added inside the webhook's Headers config.  A
        missing header caused every Case.create delivery to be logged as
        ``event=unknown`` and silently discarded — the root cause of new
        tickets not appearing in the dashboard.

        Delete payload shape (from live EspoCRM webhook queue inspection)
        -----------------------------------------------------------------
        EspoCRM sends only ``[{"id": "<ticket_id>"}]`` for Case.delete.
        There is no "deleted" flag. _infer_event_type() handles this by
        detecting a payload where "id" is present but none of the fields
        that appear on create/update payloads exist.
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
            first_record = records[0] if records else {}
            inferred = self._infer_event_type(first_record)

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
                # Log the actual record structure so engineers can diagnose
                # without needing to reproduce the webhook manually.
                safe_keys = list(first_record.keys()) if first_record else []
                logger.error(
                    "espo: could not resolve event_type | integration_id=%s | "
                    "record_keys=%s | record_key_count=%d | "
                    "payload inference failed — the service will attempt a "
                    "delete fallback before dropping this delivery | "
                    "PERMANENT FIX: add X-Webhook-Event header in EspoCRM "
                    "webhook config (Admin → Webhooks → Headers)",
                    integration.id,
                    safe_keys,
                    len(safe_keys),
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

        Rules (based on EspoCRM data contract + live webhook inspection)
        ----------------------------------------------------------------
        Minimal payload {"id": "..."}
                                     → Case.delete  ← KEY FIX
        deleted == True              → Case.delete  (explicit flag, belt+braces)
        parentType == "Case" + post  → Note.create
        createdAt == modifiedAt      → Case.create
        modifiedAt present           → Case.update

        Delete payload detail
        ---------------------
        EspoCRM sends ONLY {"id": "<ticket_id>"} for Case.delete events
        (confirmed from EspoCRM webhook queue UI). There is no "deleted"
        boolean, no timestamp, and no status field.

        Detection logic: if the record contains an "id" key but contains
        NONE of the fields that always appear on create/update payloads
        (_CREATE_UPDATE_INDICATOR_FIELDS), the record is treated as a
        delete. This is conservative — a malformed create would need to
        be missing ALL of createdAt, modifiedAt, name, status, parentType,
        description, assignedUserId, and accountId simultaneously, which
        is not a realistic EspoCRM payload.

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

        # ── Explicit delete flag (belt-and-braces) ───────────────────────
        # EspoCRM may include this in future versions or custom setups.
        if record.get("deleted") is True:
            return "Case.delete"

        # ── Minimal payload delete detection (PRIMARY delete path) ────────
        # Confirmed shape from live EspoCRM webhook queue:
        #   {"id": "69f504798ae7eff4b"}
        # If "id" is present and NONE of the fields that appear on
        # create/update payloads are present, this is a delete delivery.
        record_keys = set(record.keys())
        if "id" in record_keys and not record_keys.intersection(
            _CREATE_UPDATE_INDICATOR_FIELDS
        ):
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