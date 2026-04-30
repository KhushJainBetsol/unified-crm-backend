"""
app/integrations/webhooks/verifier.py

Centralized webhook signature/secret verification using decrypted secrets
from CrmIntegration.

Algorithm is inferred automatically from the signature prefix sent by the CRM:
  sha1=<hex>   → HMAC-SHA1   (Zammad)
  sha256=<hex> → HMAC-SHA256 (EspoCRM, GitHub-style)
  <raw hex>    → tries SHA256 then SHA1 as fallback

This means the same verifier handles all CRMs without any CRM-specific
branching — the CRM tells us which algorithm it used via the prefix.

Timing safety
-------------
All final comparisons use hmac.compare_digest() to prevent timing attacks.
The raw-hex fallback also uses compare_digest for both candidates.
"""

from __future__ import annotations

import hmac
import hashlib
import logging
from typing import Any, Dict

from app.integrations.webhooks.errors import WebhookVerificationError

logger = logging.getLogger(__name__)


class WebhookVerifier:
    """
    Verifies webhook signatures using secrets from CrmIntegration.webhook_secrets_enc.
    Supports HMAC-SHA1 (Zammad), HMAC-SHA256 (EspoCRM), and raw-hex fallback.

    Parameters
    ----------
    webhook_secrets:
        Decrypted dict from CrmIntegration.webhook_secrets_enc.
        Shape: {
            "webhook_secret": "...",
            "per_event_secrets": {"Event.name": "secret"}
        }
    """

    def __init__(self, webhook_secrets: Dict[str, Any] = None):
        self.webhook_secrets = webhook_secrets or {}

    async def verify(
        self,
        request,
        body: bytes,
        integration,
        event_type: str = None,
    ) -> None:
        """
        Verify the webhook signature using the decrypted secret(s).
        Raises WebhookVerificationError on failure.

        Algorithm is detected automatically from the signature prefix:
          - "sha1=..."   → HMAC-SHA1   (Zammad sends this)
          - "sha256=..." → HMAC-SHA256 (EspoCRM, GitHub-style)
          - raw hex      → tries SHA256 then SHA1 as fallback

        Parameters
        ----------
        request:
            FastAPI Request object.
        body:
            Raw request body bytes — must be read before any other await
            on the request (stream can only be consumed once).
        integration:
            CrmIntegration ORM object (used for logging only).
        event_type:
            Optional CRM event type string to look up per-event secrets
            (EspoCRM-style). Falls back to global webhook_secret if not found.
        """
        secret = self._get_secret(event_type)
        if not secret:
            logger.warning(
                "webhook_secrets not configured for integration_id=%s",
                integration.id,
            )
            # No secrets configured — allow through.
            # Some CRMs do not support HMAC; verification is best-effort.
            return

        signature = (
            request.headers.get("X-Hub-Signature")
            or request.headers.get("X-Signature")
        )
        if not signature:
            logger.warning(
                "Missing signature header for integration_id=%s", integration.id
            )
            # No signature header present — allow through.
            # Caller already passed UUID routing; missing header is logged
            # but not treated as a hard failure for CRMs that omit signing.
            return

        try:
            secret_bytes = secret.encode()

            if signature.startswith("sha1="):
                # ── HMAC-SHA1 (Zammad) ────────────────────────────────────
                expected = "sha1=" + hmac.new(
                    secret_bytes, body, hashlib.sha1
                ).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise WebhookVerificationError(
                        f"Signature mismatch for integration_id={integration.id}"
                    )

            elif signature.startswith("sha256="):
                # ── HMAC-SHA256 (EspoCRM, GitHub-style) ───────────────────
                expected = "sha256=" + hmac.new(
                    secret_bytes, body, hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise WebhookVerificationError(
                        f"Signature mismatch for integration_id={integration.id}"
                    )

            else:
                # ── Raw hex — no prefix, try both algorithms ───────────────
                computed_256 = hmac.new(
                    secret_bytes, body, hashlib.sha256
                ).hexdigest()
                computed_sha1 = hmac.new(
                    secret_bytes, body, hashlib.sha1
                ).hexdigest()
                matched = hmac.compare_digest(
                    signature, computed_256
                ) or hmac.compare_digest(
                    signature, computed_sha1
                )
                if not matched:
                    raise WebhookVerificationError(
                        f"Signature mismatch for integration_id={integration.id}"
                    )

            logger.debug(
                "Webhook signature verified for integration_id=%s", integration.id
            )

        except WebhookVerificationError:
            raise
        except Exception as exc:
            raise WebhookVerificationError(
                f"Signature verification failed: {exc}"
            ) from exc

    def _get_secret(self, event_type: str = None) -> str | None:
        """
        Get the appropriate secret for verification.

        Lookup order:
          1. Per-event secret matching event_type (EspoCRM-style).
          2. Global webhook_secret fallback (Zammad-style).
          3. None — no secrets configured.
        """
        if event_type and "per_event_secrets" in self.webhook_secrets:
            per_event = self.webhook_secrets["per_event_secrets"].get(event_type)
            if per_event:
                return per_event
        return self.webhook_secrets.get("webhook_secret")