"""
app/repositories/company_repository.py

Database queries for the companies table — no business logic here.

Every query uses joinedload to fetch the related source_system row
in the same SQL query, avoiding N+1 problems.

Loaded relationships per query:
  - Company.source_system → source_systems.system_name

Multitenancy:
  - Every query accepts an optional tenant_id: uuid.UUID | None.
  - When provided it is always added as a WHERE clause — this is the
    primary data-isolation guard. Never call these methods without
    passing tenant_id in a multitenant context.
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
    return select(Company).options(joinedload(Company.source_system))


class CompanyRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list all
    # ------------------------------------------------------------------

    async def get_all(
        self,
        tenant_id: uuid.UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Company], int]:
        """
        Fetch a paginated list of companies with total count.

        Args:
            tenant_id: Scope results to this tenant. Always pass this.
            offset:    Number of records to skip.
            limit:     Max records to return.

        Returns:
            Tuple of (list of Company ORM objects, total count).
        """
        count_query = select(func.count()).select_from(Company)
        query = _base_query()

        if tenant_id is not None:
            query = query.where(Company.tenant_id == tenant_id)
            count_query = count_query.where(Company.tenant_id == tenant_id)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Company.company_name.asc())
        result = await self.db.execute(query)
        companies = list(result.scalars().unique().all())

        return companies, total

    # ------------------------------------------------------------------
    # READ — list by source system
    # ------------------------------------------------------------------

    async def get_by_source_system(
        self,
        source_system_id: int,
        tenant_id: uuid.UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Company], int]:
        """
        Fetch companies belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            tenant_id:        Scope results to this tenant. Always pass this.
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
        query = _base_query().where(Company.source_system_id == source_system_id)

        if tenant_id is not None:
            query = query.where(Company.tenant_id == tenant_id)
            count_query = count_query.where(Company.tenant_id == tenant_id)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = query.offset(offset).limit(limit).order_by(Company.company_name.asc())
        result = await self.db.execute(query)
        companies = list(result.scalars().unique().all())

        return companies, total

    # ------------------------------------------------------------------
    # READ — single by internal UUID
    # ------------------------------------------------------------------

    async def get_by_id(
        self,
        company_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> Company | None:
        """
        Fetch a single company by internal UUID, scoped to tenant.

        Args:
            company_id: Internal UUID of the company.
            tenant_id:  Scope to this tenant. Always pass this.

        Returns:
            Company ORM object or None if not found (or belongs to another tenant).
        """
        query = _base_query().where(Company.id == company_id)
        if tenant_id is not None:
            query = query.where(Company.tenant_id == tenant_id)
        result = await self.db.execute(query)
        return result.scalars().first()