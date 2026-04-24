# """
# app/integrations/normalizer/espo_normalizer.py

# Converts raw EspoCRM API Case payload → NormalizedTicket.

# All status and priority mappings live in:
#   config/espo_mappings.toml

# To add a new EspoCRM status or priority — edit the TOML file only.
# No Python changes needed.
# """

# from __future__ import annotations

# import logging
# from datetime import datetime

# from app.integrations.normalizer.config.loader import get_espo_mappings
# from app.integrations.normalizer.schema import NormalizedTicket

# logger = logging.getLogger(__name__)

# DEFAULT_TITLE = "No Title"


# def _parse_datetime(value: str | None) -> datetime | None:
#     if not value:
#         return None
#     try:
#         return datetime.fromisoformat(value)
#     except (ValueError, TypeError):
#         logger.warning("Could not parse datetime value: %r", value)
#         return None


# def _derive_closed_at(status: str, modified_at: datetime) -> datetime | None:
#     """
#     EspoCRM Cases have no dedicated close timestamp.
#     Use modifiedAt as a proxy when status is closed.
#     """
#     return modified_at if status == "closed" else None


# def normalize_espo_ticket(raw: dict) -> NormalizedTicket:
#     cfg = get_espo_mappings()

#     crm_ticket_id = str(raw["id"])
#     created_at    = datetime.fromisoformat(raw["createdAt"])
#     updated_at    = datetime.fromisoformat(raw["modifiedAt"])

#     # status — read from config
#     raw_status = str(raw.get("status", "")).lower().strip()
#     status = cfg.status.get(raw_status, cfg.fallback_status)
#     if raw_status and raw_status not in cfg.status:
#         logger.warning(
#             "Unknown EspoCRM status %r for case %s — using fallback %r",
#             raw_status, crm_ticket_id, cfg.fallback_status,
#         )

#     # priority — read from config
#     raw_priority = str(raw.get("priority", "")).lower().strip()
#     priority = cfg.priority.get(raw_priority, cfg.fallback_priority) or None
#     if raw_priority and raw_priority not in cfg.priority:
#         logger.warning(
#             "Unknown EspoCRM priority %r for case %s — using fallback",
#             raw_priority, crm_ticket_id,
#         )

#     return NormalizedTicket(
#         crm_ticket_id=crm_ticket_id,
#         source_system="espocrm",
#         title=(raw.get("name") or DEFAULT_TITLE).strip(),
#         description=raw.get("description") or None,
#         status=status,
#         priority=priority,
#         crm_agent_id=raw.get("assignedUserId") or None,
#         crm_customer_id=raw.get("createdById") or None,
#         crm_company_id=raw.get("accountId") or None,
#         created_at=created_at,
#         updated_at=updated_at,
#         closed_at=_derive_closed_at(status, updated_at),
#     )


# def normalize_espo_tickets(raw_list: list[dict]) -> list[NormalizedTicket]:
#     results: list[NormalizedTicket] = []
#     for raw in raw_list:
#         try:
#             results.append(normalize_espo_ticket(raw))
#         except (KeyError, ValueError) as exc:
#             logger.error(
#                 "Failed to normalize EspoCRM case id=%r: %s",
#                 raw.get("id"), exc,
#             )
#     return results