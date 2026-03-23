"""
app/repositories/agent_repository.py

Database queries for the agents table — no business logic here.

Every query that returns a response uses joinedload to fetch the
related source_system row in the same SQL query, avoiding N+1 problems.

Loaded relationships per query:
  - Agent.source_system → source_systems.system_name
  - Agent.tickets       → NOT loaded here (use ticket repo for that)
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.agent import Agent


def _base_query():
    """
    Base SELECT with all joinedloads applied.
    Every read query builds on top of this so joins are never forgotten.
    """
    return (
        select(Agent)
        .options(
            joinedload(Agent.source_system),
        )
    )


class AgentRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list
    # ------------------------------------------------------------------
    async def get_all(
        self,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Agent], int]:
        """
        Fetch a paginated list of agents with total count.

        Args:
            include_inactive: If False (default) only returns active agents.
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Agent ORM objects, total count).
        """
        query = _base_query()
        count_query = select(func.count()).select_from(Agent)

        if not include_inactive:
            query = query.where(Agent.is_active == True)   # noqa: E712
            count_query = count_query.where(Agent.is_active == True)  # noqa: E712

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Agent.name.asc())
        result = await self.db.execute(query)
        agents = list(result.scalars().unique().all())

        return agents, total

    async def get_by_source_system(
        self,
        source_system_id: int,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Agent], int]:
        """
        Fetch agents belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            include_inactive: If False excludes inactive agents.
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Agent ORM objects, total count).
        """
        query = _base_query().where(Agent.source_system_id == source_system_id)
        count_query = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.source_system_id == source_system_id)
        )

        if not include_inactive:
            query = query.where(Agent.is_active == True)   # noqa: E712
            count_query = count_query.where(Agent.is_active == True)  # noqa: E712

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Agent.name.asc())
        result = await self.db.execute(query)
        agents = list(result.scalars().unique().all())

        return agents, total

    # ------------------------------------------------------------------
    # READ — single
    # ------------------------------------------------------------------
    async def get_by_id(self, agent_id: uuid.UUID) -> Agent | None:
        """
        Fetch a single agent by internal UUID.

        Returns:
            Agent ORM object or None if not found.
        """
        query = _base_query().where(Agent.id == agent_id)
        result = await self.db.execute(query)
        return result.scalars().first()