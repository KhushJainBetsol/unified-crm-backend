"""
app/integrations/zammad/service.py

Zammad integration service.

Sits between the Zammad HTTP client and the rest of your application.

Responsibilities:
  - Call ZammadClient to fetch raw ticket data
  - Pass raw data through the normalizer
  - Return NormalizedTicket objects ready for the sync service

This layer knows about normalisation but nothing about your DB.
DB persistence is handled by the sync service (services/sync_service.py).
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.integrations.normalizer import NormalizedTicket, normalize_tickets
from app.integrations.zammad.client import ZammadClient, ZammadClientError

logger = logging.getLogger(__name__)


class ZammadService:
    """
    Zammad integration service.

    Usage:
        async with ZammadClient() as client:
            service = ZammadService(client)
            tickets = await service.fetch_all_tickets()
    """

    def __init__(self, client: ZammadClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Ticket fetching
    # ------------------------------------------------------------------
    async def fetch_all_tickets(self) -> list[NormalizedTicket]:
        """
        Fetch and normalize ALL tickets from Zammad.
        Use for initial full sync.

        Returns:
            List of NormalizedTicket — ready to upsert into your DB.
        """
        logger.info("Starting full Zammad ticket sync")

        try:
            raw_tickets = await self._client.get_all_tickets()
        except ZammadClientError as exc:
            logger.error("Failed to fetch tickets from Zammad: %s", exc)
            raise

        normalized = normalize_tickets(raw_tickets, source_system="zammad")

        logger.info(
            "Zammad full sync: %d raw → %d normalized tickets",
            len(raw_tickets),
            len(normalized),
        )
        return normalized

    async def fetch_ticket_by_id(self, ticket_id: int | str) -> NormalizedTicket | None:
        """
        Fetch and normalize a single Zammad ticket by ID.
        Returns None if the ticket is not found.

        Args:
            ticket_id: Zammad ticket integer ID.

        Returns:
            NormalizedTicket or None.
        """
        try:
            raw = await self._client.get_ticket_by_id(ticket_id)
        except ZammadClientError as exc:
            logger.error("Failed to fetch Zammad ticket %s: %s", ticket_id, exc)
            return None

        try:
            from app.integrations.normalizer.zammad_normalizer import normalize_zammad_ticket
            return normalize_zammad_ticket(raw)
        except (KeyError, ValueError) as exc:
            logger.error(
                "Failed to normalize Zammad ticket %s: %s", ticket_id, exc
            )
            return None

    async def fetch_tickets_since(
        self,
        since: datetime,
    ) -> list[NormalizedTicket]:
        """
        Fetch and normalize only tickets updated after `since`.
        Use for incremental sync after the first full sync.

        Args:
            since: Fetch tickets updated after this datetime.

        Returns:
            List of NormalizedTicket objects.
        """
        since_str = since.isoformat()
        logger.info("Zammad incremental sync since %s", since_str)

        try:
            raw_tickets = await self._client.get_tickets_updated_since(since_str)
        except ZammadClientError as exc:
            logger.error(
                "Failed to fetch Zammad tickets since %s: %s", since_str, exc
            )
            raise

        normalized = normalize_tickets(raw_tickets, source_system="zammad")

        logger.info(
            "Zammad incremental sync: %d raw → %d normalized tickets",
            len(raw_tickets),
            len(normalized),
        )
        return normalized
