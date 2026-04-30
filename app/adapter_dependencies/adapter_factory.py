"""
app/adapter_dependencies/adapter_factory.py

Simplified FastAPI dependency for TicketService.

PHASE 1: Consolidated adapter infrastructure.
This file now contains only the high-level TicketService dependency.
All adapter bootstrap is in app/main.py.
All adapter DI is in app/adapter_dependencies/deps.py.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapter_dependencies.deps import get_adapter_factory
from app.dependencies import get_db
from app.factory.adapter_factory import CrmAdapterFactory
from app.services.ticket_service import TicketService


async def get_ticket_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    factory: Annotated[CrmAdapterFactory, Depends(get_adapter_factory)],
) -> TicketService:
    """
    FastAPI dependency that constructs TicketService with the main
    CrmAdapterFactory from app.state (bootstrapped in app/main.py).

    This is the canonical way for routes to get an injected TicketService.

    Usage in routes:
        @router.get("/tickets")
        async def list_tickets(
            service: TicketService = Depends(get_ticket_service),
        ):
            tickets, total = await service.get_tickets(...)
            return paginated(items=tickets, total=total, ...)
    """
    return TicketService(db=db, adapter_factory=factory)