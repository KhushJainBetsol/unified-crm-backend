"""
app/integrations/webhooks/handlers/__init__.py
"""

from __future__ import annotations

from app.integrations.webhooks.base import BaseWebhookHandler
from app.integrations.webhooks.handlers.espo import EspoWebhookHandler
from app.integrations.webhooks.handlers.zammad import ZammadWebhookHandler

HANDLERS: dict[str, BaseWebhookHandler] = {
    "espocrm": EspoWebhookHandler(),
    "zammad": ZammadWebhookHandler(),
}


def get_handler(system_name: str) -> BaseWebhookHandler | None:
    return HANDLERS.get(system_name.lower())
