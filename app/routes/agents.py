"""
app/routes/agents.py  — UPDATED for multitenancy

Same pattern as tickets.py — get_current_user injected, tenant_id passed down.
Existing logic unchanged.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
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


@router.get("/", summary="List all agents for current tenant")
async def list_agents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    agents, total = await AgentService(db).get_agents(
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message="Agents fetched successfully",
    )


@router.get("/filter", summary="Filter agents")
async def filter_agents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = Query(default=False),
    source: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    agents, total = await AgentService(db).filter_agents(
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
        source=source,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message="Agents fetched successfully",
    )


@router.get("/{agent_id}", summary="Get agent by ID")
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    agent = await AgentService(db).get_agent_or_404(
        agent_id,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return success("Agent fetched successfully", _to_response(agent))