"""
app/integrations/normalizer/__init__.py

Public API for the normalizer package.
Import only from here — not from individual normalizer files.

Usage:
    from app.integrations.normalizer import normalize_ticket, normalize_tickets, NormalizedTicket
"""

from app.integrations.normalizer.registry import (
    get_supported_sources,
    normalize_ticket,
    normalize_ticket_with_registry,
    normalize_tickets,
    normalize_tickets_with_registry,
)
from app.integrations.normalizer.schema import NormalizedTicket

__all__ = [
    "NormalizedTicket",
    "normalize_ticket",
    "normalize_tickets",
    "normalize_ticket_with_registry",
    "normalize_tickets_with_registry",
    "get_supported_sources",
]
