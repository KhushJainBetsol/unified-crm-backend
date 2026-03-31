"""
app/integrations/espo/service.py

EspoCRM integration service.

Sits between the EspoCRM HTTP client and the rest of your application.

Responsibilities:
  - Call EspoClient to fetch raw case data
  - Pass raw data through the normalizer
  - Return NormalizedTicket objects ready for the sync service

This layer knows about normalisation but nothing about your DB.
DB persistence is handled by the sync service (services/sync_service.py).
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.integrations.espo.client import EspoClient, EspoClientError
from app.integrations.normalizer import NormalizedTicket, normalize_tickets

logger = logging.getLogger(__name__)


class EspoService:
    """
    EspoCRM integration service.

    Usage:
        async with EspoClient() as client:
            service = EspoService(client)
            tickets = await service.fetch_all_tickets()
    """

    def __init__(self, client: EspoClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Ticket fetching
    # ------------------------------------------------------------------
    async def fetch_all_tickets(self) -> list[NormalizedTicket]:
        """
        Fetch and normalize ALL Cases from EspoCRM.
        Use for initial full sync.

        Returns:
            List of NormalizedTicket — ready to upsert into your DB.
        """
        logger.info("Starting full EspoCRM ticket sync")

        try:
            raw_tickets = await self._client.get_all_tickets()
        except EspoClientError as exc:
            logger.error("Failed to fetch cases from EspoCRM: %s", exc)
            raise

        normalized = normalize_tickets(raw_tickets, source_system="espocrm")

        logger.info(
            "EspoCRM full sync: %d raw → %d normalized tickets",
            len(raw_tickets),
            len(normalized),
        )
        return normalized

    async def fetch_ticket_by_id(self, ticket_id: str) -> NormalizedTicket | None:
        """
        Fetch and normalize a single EspoCRM Case by ID.
        Returns None if the case is not found or fails normalisation.

        Args:
            ticket_id: EspoCRM Case UUID string.

        Returns:
            NormalizedTicket or None.
        """
        try:
            raw = await self._client.get_ticket_by_id(ticket_id)
        except EspoClientError as exc:
            logger.error("Failed to fetch EspoCRM case %s: %s", ticket_id, exc)
            return None

        try:
            from app.integrations.normalizer.espo_normalizer import (
                normalize_espo_ticket,
            )

            return normalize_espo_ticket(raw)
        except (KeyError, ValueError) as exc:
            logger.error("Failed to normalize EspoCRM case %s: %s", ticket_id, exc)
            return None

    async def fetch_tickets_since(
        self,
        since: datetime,
    ) -> list[NormalizedTicket]:
        """
        Fetch and normalize only Cases modified after `since`.
        Use for incremental sync after the first full sync.

        Args:
            since: Fetch cases modified after this datetime.

        Returns:
            List of NormalizedTicket objects.
        """
        since_str = since.isoformat()
        logger.info("EspoCRM incremental sync since %s", since_str)

        try:
            raw_tickets = await self._client.get_tickets_updated_since(since_str)
        except EspoClientError as exc:
            logger.error("Failed to fetch EspoCRM cases since %s: %s", since_str, exc)
            raise

        normalized = normalize_tickets(raw_tickets, source_system="espocrm")

        logger.info(
            "EspoCRM incremental sync: %d raw → %d normalized tickets",
            len(raw_tickets),
            len(normalized),
        )
        return normalized

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_case_field_options(self) -> dict[str, list[str]]:
        """
        Return the valid option values for Case fields from EspoCRM metadata.

        Use this to discover what status / priority values your specific
        EspoCRM instance accepts — admins can customise these via the UI
        so they differ between installations.

        Returns:
            Dict of field_name → list of valid option strings.
            e.g. {"status": ["New", "Assigned", "Closed", ...], ...}
        """
        return await self._client.get_case_field_options()

    # ------------------------------------------------------------------
    # Ticket update (push change to EspoCRM)
    # ------------------------------------------------------------------
    async def update_ticket(
        self,
        crm_ticket_id: str,
        data: dict,
    ) -> dict:
        """
        Push a field update for an existing EspoCRM Case.

        Args:
            crm_ticket_id: EspoCRM Case UUID string (stored as crm_ticket_id in our DB).
            data:          EspoCRM-native field dict, e.g.
                           {
                               "status":         "Closed",
                               "priority":       "High",
                               "assignedUserId": "abc123-espo-uuid"
                           }

        Returns:
            Raw updated Case dict from EspoCRM.

        Raises:
            EspoClientError: on HTTP errors from EspoCRM.
        """
        logger.info("EspoCRM update_ticket crm_id=%s data=%s", crm_ticket_id, data)
        return await self._client.update_ticket(crm_ticket_id, data)
