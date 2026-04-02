"""
app/integrations/webhooks/base.py
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import Request

from app.integrations.webhooks.models import RawWebhookPayload
from app.models.crm_integration import CrmIntegration


class BaseWebhookHandler(ABC):

    @abstractmethod
    async def verify(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> None:
        """
        Verify the request is genuine.
        Secrets come from integration — never from settings.
        Raise HTTPException(401) on failure.
        """

    @abstractmethod
    async def parse(
        self,
        request: Request,
        body: bytes,
        integration: CrmIntegration,
    ) -> RawWebhookPayload:
        """
        Parse raw body into RawWebhookPayload.
        Copies only plain values out of integration (id, source_system_id, etc.)
        so the returned dataclass has zero dependency on the SQLAlchemy session.
        Raise HTTPException(400) on bad input.
        """
