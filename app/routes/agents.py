"""
app/routes/agents.py

GET /agents/                        → paginated list   (AgentResponse)
GET /agents/source/{source_system}  → filtered by CRM  (AgentResponse)
GET /agents/{id}                    → full detail       (AgentResponse)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.source_system import SourceSystem
from app.repositories.agent_repository import AgentRepository
from app.schemas.agent import AgentResponse
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["Agents"])


# ---------------------------------------------------------------------------
# Mapper — ORM object → Pydantic schema dict
# ---------------------------------------------------------------------------

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
# Helper — resolve source system name → DB row
# ---------------------------------------------------------------------------

async def _get_source_system(name: str, db: AsyncSession):
    result = await db.execute(
        select(SourceSystem).where(SourceSystem.system_name == name.lower())
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# GET /agents/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all agents")
async def list_agents(
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    include_inactive: bool = Query(default=False, description="Include inactive agents"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    agents, total = await AgentRepository(db).get_all(
        include_inactive=include_inactive,
        offset=offset,
        limit=page_size,
    )
    logger.debug("list_agents: returned %d of %d total", len(agents), total)
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message="Agents fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /agents/source/{source_system_name}
# IMPORTANT: must be defined BEFORE /{agent_id} so FastAPI does not
# try to parse the literal string "source" as a UUID
# ---------------------------------------------------------------------------

@router.get("/source/{source_system_name}", summary="List agents by CRM source")
async def list_agents_by_source(
    source_system_name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    source = await _get_source_system(source_system_name, db)

    if not source:
        logger.warning("list_agents_by_source: unknown source '%s'", source_system_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source system '{source_system_name}' not found. Valid values: zammad, espocrm",
        )

    offset = (page - 1) * page_size
    agents, total = await AgentRepository(db).get_by_source_system(
        source_system_id=source.id,
        include_inactive=include_inactive,
        offset=offset,
        limit=page_size,
    )
    logger.debug(
        "list_agents_by_source: source=%s returned %d of %d",
        source_system_name, len(agents), total,
    )
    return paginated(
        items=[_to_response(a) for a in agents],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Agents for '{source_system_name}' fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /agents/{agent_id}
# ---------------------------------------------------------------------------

@router.get("/{agent_id}", summary="Get agent by ID")
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    agent = await AgentRepository(db).get_by_id(agent_id)

    if not agent:
        logger.warning("get_agent: agent %s not found", agent_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    return success("Agent fetched successfully", _to_response(agent))