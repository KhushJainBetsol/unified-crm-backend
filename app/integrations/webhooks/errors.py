"""
app/integrations/webhooks/errors.py
Centralized error hierarchy for webhook processing.
"""


class WebhookError(Exception):
    """Base class for all webhook-related errors."""
    pass


class WebhookVerificationError(WebhookError):
    """Raised when webhook signature/secret verification fails."""
    pass


class WebhookParseError(WebhookError):
    """Raised when webhook payload is malformed or cannot be parsed."""
    pass


class WebhookAdapterError(WebhookError):
    """Raised when adapter creation or authentication fails."""
    pass


class WebhookSyncError(WebhookError):
    """Raised when ticket normalization or persistence fails."""
    pass


class WebhookCommentError(WebhookError):
    """Raised when comment fetch or persistence fails."""
    pass
