"""
app/repositories/company_repository.py

Database queries for the companies table — no business logic here.

Every query uses joinedload to fetch the related source_system row
in the same SQL query, avoiding N+1 problems.

Loaded relationships per query:
  - Company.source_system → source_systems.system_name
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.company import Company


def _base_query():
    """
    Base SELECT with all joinedloads applied.
    Every read query builds on top of this so joins are never forgotten.
    """
    return (
        select(Company)
        .options(
            joinedload(Company.source_system),
        )
    )


class CompanyRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list all
    # ------------------------------------------------------------------
    async def get_all(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Company], int]:
        """
        Fetch a paginated list of all companies with total count.

        Args:
            offset: Number of records to skip.
            limit:  Max records to return.

        Returns:
            Tuple of (list of Company ORM objects, total count).
        """
        count_query = select(func.count()).select_from(Company)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            _base_query()
            .offset(offset)
            .limit(limit)
            .order_by(Company.company_name.asc())
        )
        result = await self.db.execute(query)
        companies = list(result.scalars().unique().all())

        return companies, total

    # ------------------------------------------------------------------
    # READ — list by source system
    # ------------------------------------------------------------------
    async def get_by_source_system(
        self,
        source_system_id: int,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Company], int]:
        """
        Fetch companies belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Company ORM objects, total count).
        """
        count_query = (
            select(func.count())
            .select_from(Company)
            .where(Company.source_system_id == source_system_id)
        )
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            _base_query()
            .where(Company.source_system_id == source_system_id)
            .offset(offset)
            .limit(limit)
            .order_by(Company.company_name.asc())
        )
        result = await self.db.execute(query)
        companies = list(result.scalars().unique().all())

        return companies, total

    # ------------------------------------------------------------------
    # READ — single by internal UUID
    # ------------------------------------------------------------------
    async def get_by_id(self, company_id: uuid.UUID) -> Company | None:
        """
        Fetch a single company by internal UUID.

        Returns:
            Company ORM object or None if not found.
        """
        query = _base_query().where(Company.id == company_id)
        result = await self.db.execute(query)
        return result.scalars().first()