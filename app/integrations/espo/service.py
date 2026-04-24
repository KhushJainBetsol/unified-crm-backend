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
from pathlib import Path

from app.integrations.espo.client import EspoClient, EspoClientError
from app.integrations.normalizer import NormalizedTicket
from app.integrations.normalizer.normalizer import normalize_ticket, normalize_tickets
from app.config.loader import ConfigLoader

logger = logging.getLogger(__name__)

_CONFIG_BASE_DIR = Path(__file__).parent.parent.parent / "config"


def _get_espo_config():
    return ConfigLoader(base_dir=_CONFIG_BASE_DIR).load_adapter_config("espocrm/config.yaml")


class EspoService:

    def __init__(self, client: EspoClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Ticket fetching
    # ------------------------------------------------------------------
    async def fetch_all_tickets(self) -> list[NormalizedTicket]:
        """Fetch and normalize ALL Cases from EspoCRM (full sync, all accounts)."""
        logger.info("Starting full EspoCRM ticket sync")
        try:
            raw_tickets = await self._client.get_all_tickets()
        except EspoClientError as exc:
            logger.error("Failed to fetch cases from EspoCRM: %s", exc)
            raise
        config = _get_espo_config()
        normalized = normalize_tickets(raw_tickets, "espocrm", config)
        logger.info(
            "EspoCRM full sync: %d raw → %d normalized tickets",
            len(raw_tickets), len(normalized),
        )
        return normalized

    def normalize_raw_tickets(self, raw_tickets: list[dict]) -> list[NormalizedTicket]:
        """
        Normalize an already-fetched list of raw EspoCRM case dicts.

        Used by the multitenant scheduler after fetching account-scoped cases.
        """
        config = _get_espo_config()
        normalized = normalize_tickets(raw_tickets, "espocrm", config)
        logger.info(
            "EspoCRM normalize_raw_tickets: %d raw → %d normalized",
            len(raw_tickets), len(normalized),
        )
        return normalized

    async def fetch_ticket_by_id(self, ticket_id: str) -> NormalizedTicket | None:
        try:
            raw = await self._client.get_ticket_by_id(ticket_id)
        except EspoClientError as exc:
            logger.error("Failed to fetch EspoCRM case %s: %s", ticket_id, exc)
            return None
        try:
            config = _get_espo_config()
            return normalize_ticket(raw, "espocrm", config)
        except (KeyError, ValueError) as exc:
            logger.error("Failed to normalize EspoCRM case %s: %s", ticket_id, exc)
            return None

    async def fetch_tickets_since(self, since: datetime) -> list[NormalizedTicket]:
        since_str = since.isoformat()
        logger.info("EspoCRM incremental sync since %s", since_str)
        try:
            raw_tickets = await self._client.get_tickets_updated_since(since_str)
        except EspoClientError as exc:
            logger.error("Failed to fetch EspoCRM cases since %s: %s", since_str, exc)
            raise
        config = _get_espo_config()
        normalized = normalize_tickets(raw_tickets, "espocrm", config)
        logger.info(
            "EspoCRM incremental sync: %d raw → %d normalized tickets",
            len(raw_tickets), len(normalized),
        )
        return normalized

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_case_field_options(self) -> dict[str, list[str]]:
        return await self._client.get_case_field_options()

    # ------------------------------------------------------------------
    # Ticket update (push change to EspoCRM)
    # ------------------------------------------------------------------
    async def update_ticket(self, crm_ticket_id: str, data: dict) -> dict:
        logger.info("EspoCRM update_ticket crm_id=%s data=%s", crm_ticket_id, data)
        return await self._client.update_ticket(crm_ticket_id, data)