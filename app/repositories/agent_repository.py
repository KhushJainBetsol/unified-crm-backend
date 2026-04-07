from __future__ import annotations
import uuid
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from app.models.agent import Agent


def _base_query(tenant_id: uuid.UUID):
    """
    Base SELECT with tenant isolation and joinedload applied.
    """
    return (
        select(Agent)
        .where(Agent.tenant_id == tenant_id)
        .options(joinedload(Agent.source_system))
    )


class AgentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_all(
        self,
        tenant_id: uuid.UUID,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Agent], int]:
        query = _base_query(tenant_id)
        count_query = (
            select(func.count()).select_from(Agent).where(Agent.tenant_id == tenant_id)
        )

        if not include_inactive:
            query = query.where(Agent.is_active == True)
            count_query = count_query.where(Agent.is_active == True)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Agent.name.asc())
        result = await self.db.execute(query)
        agents = list(result.scalars().unique().all())

        return agents, total

    async def get_by_source_system(
        self,
        tenant_id: uuid.UUID,
        source_system_id: int,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Agent], int]:
        query = _base_query(tenant_id).where(Agent.source_system_id == source_system_id)
        count_query = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.tenant_id == tenant_id)
            .where(Agent.source_system_id == source_system_id)
        )

        if not include_inactive:
            query = query.where(Agent.is_active == True)
            count_query = count_query.where(Agent.is_active == True)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Agent.name.asc())
        result = await self.db.execute(query)
        agents = list(result.scalars().unique().all())

        return agents, total

    async def get_by_id(
        self, agent_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Agent | None:
        query = _base_query(tenant_id).where(Agent.id == agent_id)
        result = await self.db.execute(query)
        return result.scalars().first()
