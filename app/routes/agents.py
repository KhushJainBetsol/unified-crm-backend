"""
app/routes/agents.py

GET /agents/         → paginated list
GET /agents/filter   → filtered list (?source ?include_inactive)
GET /agents/{id}     → full detail
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.agent import AgentResponse
from app.services.agent_service import AgentService
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["Agents"])


def _to_response(agent) -> dict:
    return AgentResponse(
        id=agent.id,
        crm_agent_id=agent.crm_agent_id,
        source_system=agent.source_system.system_name,
        name=agent.name,
        email=agent.email,
        is_active=agent.is_active,
    ).model_dump()


# ---------------------------------------------------------------------------
# GET /agents/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all agents")
async def list_agents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = Query(default=False, description="Include inactive agents"),
    db: AsyncSession = Depends(get_db),
):
    agents, total = await AgentService(db).get_agents(
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
    )
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message="Agents fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /agents/filter
# NOTE: defined before /{agent_id} so "filter" is not parsed as a UUID
# ---------------------------------------------------------------------------

@router.get("/filter", summary="Filter agents by source or active status")
async def filter_agents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = Query(default=False),
    source: str | None = Query(default=None, description="CRM source: zammad, espocrm"),
    db: AsyncSession = Depends(get_db),
):
    agents, total = await AgentService(db).filter_agents(
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        source=source,
    )
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message="Agents fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /agents/{agent_id}
# ---------------------------------------------------------------------------

@router.get("/{agent_id}", summary="Get agent by ID")
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    agent = await AgentService(db).get_agent_or_404(agent_id)
    return success("Agent fetched successfully", _to_response(agent))