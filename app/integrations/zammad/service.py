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
from pathlib import Path

from app.integrations.normalizer import NormalizedTicket
from app.integrations.normalizer.normalizer import normalize_ticket, normalize_tickets
from app.integrations.zammad.client import ZammadClient, ZammadClientError
from app.config.loader import ConfigLoader

logger = logging.getLogger(__name__)

_CONFIG_BASE_DIR = Path(__file__).parent.parent.parent / "config"


def _get_zammad_config():
    return ConfigLoader(base_dir=_CONFIG_BASE_DIR).load_adapter_config("zammad/config.yaml")


class ZammadService:

    def __init__(self, client: ZammadClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Ticket fetching
    # ------------------------------------------------------------------
    async def fetch_all_tickets(self) -> list[NormalizedTicket]:
        """Fetch and normalize ALL tickets from Zammad (full sync, all orgs)."""
        logger.info("Starting full Zammad ticket sync")
        try:
            raw_tickets = await self._client.get_all_tickets()
        except ZammadClientError as exc:
            logger.error("Failed to fetch tickets from Zammad: %s", exc)
            raise
        config = _get_zammad_config()
        normalized = normalize_tickets(raw_tickets, "zammad", config)
        logger.info(
            "Zammad full sync: %d raw → %d normalized tickets",
            len(raw_tickets), len(normalized),
        )
        return normalized

    def normalize_raw_tickets(self, raw_tickets: list[dict]) -> list[NormalizedTicket]:
        """
        Normalize an already-fetched list of raw Zammad ticket dicts.

        Used by the multitenant scheduler after fetching org-scoped tickets.
        """
        config = _get_zammad_config()
        normalized = normalize_tickets(raw_tickets, "zammad", config)
        logger.info(
            "Zammad normalize_raw_tickets: %d raw → %d normalized",
            len(raw_tickets), len(normalized),
        )
        return normalized

    async def fetch_ticket_by_id(self, ticket_id: int | str) -> NormalizedTicket | None:
        try:
            raw = await self._client.get_ticket_by_id(ticket_id)
        except ZammadClientError as exc:
            logger.error("Failed to fetch Zammad ticket %s: %s", ticket_id, exc)
            return None
        try:
            config = _get_zammad_config()
            return normalize_ticket(raw, "zammad", config)
        except (KeyError, ValueError) as exc:
            logger.error("Failed to normalize Zammad ticket %s: %s", ticket_id, exc)
            return None

    async def fetch_tickets_since(self, since: datetime) -> list[NormalizedTicket]:
        since_str = since.isoformat()
        logger.info("Zammad incremental sync since %s", since_str)
        try:
            raw_tickets = await self._client.get_tickets_updated_since(since_str)
        except ZammadClientError as exc:
            logger.error("Failed to fetch Zammad tickets since %s: %s", since_str, exc)
            raise
        config = _get_zammad_config()
        normalized = normalize_tickets(raw_tickets, "zammad", config)
        logger.info(
            "Zammad incremental sync: %d raw → %d normalized tickets",
            len(raw_tickets), len(normalized),
        )
        return normalized

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_ticket_field_options(self) -> dict[str, list[str]]:
        return await self._client.get_ticket_field_options()

    # ------------------------------------------------------------------
    # Ticket update (push change to Zammad)
    # ------------------------------------------------------------------
    async def update_ticket(self, crm_ticket_id: str | int, data: dict) -> dict:
        logger.info("Zammad update_ticket crm_id=%s data=%s", crm_ticket_id, data)
        return await self._client.update_ticket(crm_ticket_id, data)