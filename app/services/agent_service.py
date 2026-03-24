"""
app/services/agent_service.py

Business logic for agents — sits between routes and repositories.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.source_system import SourceSystem
from app.repositories.agent_repository import AgentRepository

logger = logging.getLogger(__name__)


class AgentService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AgentRepository(db)

    # ------------------------------------------------------------------
    # Source system helper
    # ------------------------------------------------------------------

    async def _resolve_source_system(self, source: str):
        result = await self.db.execute(
            select(SourceSystem).where(SourceSystem.system_name == source.lower())
        )
        source_obj = result.scalars().first()
        if not source_obj:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Source system '{source}' not found. Valid values: zammad, espocrm",
            )
        return source_obj

    # ------------------------------------------------------------------
    # List / filter
    # ------------------------------------------------------------------

    async def get_agents(
        self,
        page: int,
        page_size: int,
        include_inactive: bool = False,
    ) -> tuple[list, int]:
        offset = (page - 1) * page_size
        return await self.repo.get_all(
            include_inactive=include_inactive,
            offset=offset,
            limit=page_size,
        )

    async def filter_agents(
        self,
        page: int,
        page_size: int,
        include_inactive: bool = False,
        source: str | None = None,
    ) -> tuple[list, int]:
        offset = (page - 1) * page_size

        if source:
            source_obj = await self._resolve_source_system(source)
            return await self.repo.get_by_source_system(
                source_system_id=source_obj.id,
                include_inactive=include_inactive,
                offset=offset,
                limit=page_size,
            )

        return await self.repo.get_all(
            include_inactive=include_inactive,
            offset=offset,
            limit=page_size,
        )

    # ------------------------------------------------------------------
    # Single agent
    # ------------------------------------------------------------------

    async def get_agent_or_404(self, agent_id: uuid.UUID) -> Agent:
        agent = await self.repo.get_by_id(agent_id)
        if not agent:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Agent {agent_id} not found",
            )
        return agent