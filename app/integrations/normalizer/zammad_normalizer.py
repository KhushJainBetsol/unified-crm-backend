# """
# app/integrations/normalizer/zammad_normalizer.py

# Converts raw Zammad API ticket payload → NormalizedTicket.

# All status and priority mappings live in:
#   config/zammad_mappings.toml

# To add a new Zammad state or priority — edit the TOML file only.
# No Python changes needed.
# """

# from __future__ import annotations

# import logging
# from datetime import datetime

# from app.integrations.normalizer.config.loader import get_zammad_mappings
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


# def _resolve_priority(raw: dict) -> str | None:
#     """
#     Resolve priority from Zammad ticket dict.

#     Zammad list endpoint returns:   priority_id = 2  (integer)
#     Zammad single endpoint returns: priority = "2 normal" or priority = 2

#     Checks priority_id first, then falls back to priority string.
#     All mappings loaded from zammad_mappings.toml.
#     """
#     cfg = get_zammad_mappings()

#     # Format 1 — integer ID from list endpoint (priority_id field)
#     priority_id = raw.get("priority_id")
#     if priority_id is not None:
#         try:
#             result = cfg.priority_id.get(int(priority_id))
#             if result:
#                 return result
#         except (ValueError, TypeError):
#             pass

#     # Format 2 & 3 — string from single ticket endpoint (priority field)
#     priority = raw.get("priority")
#     if priority is None:
#         return cfg.fallback_priority

#     if isinstance(priority, int):
#         return cfg.priority_id.get(priority, cfg.fallback_priority)

#     if isinstance(priority, str):
#         result = cfg.priority_name.get(priority.lower().strip())
#         if result:
#             return result
#         # last attempt — extract numeric prefix e.g. "2 normal" → 2
#         parts = priority.strip().split()
#         if parts and parts[0].isdigit():
#             result = cfg.priority_id.get(int(parts[0]))
#             if result:
#                 return result

#     logger.warning(
#         "Could not resolve Zammad priority from raw value: priority_id=%r priority=%r",
#         raw.get("priority_id"), raw.get("priority"),
#     )
#     return cfg.fallback_priority


# def normalize_zammad_ticket(raw: dict) -> NormalizedTicket:
#     cfg = get_zammad_mappings()

#     crm_ticket_id = str(raw["id"])
#     created_at    = datetime.fromisoformat(raw["created_at"])
#     updated_at    = datetime.fromisoformat(raw["updated_at"])

#     # status — read from config
#     raw_state = str(raw.get("state", "")).lower().strip()
#     status = cfg.status.get(raw_state, cfg.fallback_status)
#     if raw_state and raw_state not in cfg.status:
#         logger.warning(
#             "Unknown Zammad state %r for ticket %s — using fallback %r",
#             raw_state, crm_ticket_id, cfg.fallback_status,
#         )

#     return NormalizedTicket(
#         crm_ticket_id=crm_ticket_id,
#         source_system="zammad",
#         title=(raw.get("title") or DEFAULT_TITLE).strip(),
#         description=raw.get("note") or raw.get("body") or None,
#         status=status,
#         priority=_resolve_priority(raw),
#         crm_agent_id=str(raw["owner_id"]) if raw.get("owner_id") else None,
#         crm_customer_id=str(raw["customer_id"]) if raw.get("customer_id") else None,
#         crm_company_id=str(raw["organization_id"]) if raw.get("organization_id") else None,
#         created_at=created_at,
#         updated_at=updated_at,
#         closed_at=_parse_datetime(raw.get("close_at")),
#     )


# def normalize_zammad_tickets(raw_list: list[dict]) -> list[NormalizedTicket]:
#     results: list[NormalizedTicket] = []
#     for raw in raw_list:
#         try:
#             results.append(normalize_zammad_ticket(raw))
#         except (KeyError, ValueError) as exc:
#             logger.error(
#                 "Failed to normalize Zammad ticket id=%r: %s",
#                 raw.get("id"), exc,
#             )
#     return results