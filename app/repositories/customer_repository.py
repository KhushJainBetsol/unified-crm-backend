"""
app/repositories/customer_repository.py

Database queries for the customers table — no business logic here.

Every query uses joinedload to fetch related rows in the same SQL
query, avoiding N+1 problems.

Loaded relationships per query:
  - Customer.source_system → source_systems.system_name
  - Customer.company       → companies (optional, may be NULL)
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.customer import Customer


def _base_query():
    """
    Base SELECT with all joinedloads applied.
    Every read query builds on top of this so joins are never forgotten.
    """
    return (
        select(Customer)
        .options(
            joinedload(Customer.source_system),
            joinedload(Customer.company),
        )
    )


class CustomerRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # READ — list all
    # ------------------------------------------------------------------
    async def get_all(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Customer], int]:
        """
        Fetch a paginated list of all customers with total count.

        Args:
            offset: Number of records to skip.
            limit:  Max records to return.

        Returns:
            Tuple of (list of Customer ORM objects, total count).
        """
        count_query = select(func.count()).select_from(Customer)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            _base_query()
            .offset(offset)
            .limit(limit)
            .order_by(Customer.first_name.asc(), Customer.last_name.asc())
        )
        result = await self.db.execute(query)
        customers = list(result.scalars().unique().all())

        return customers, total

    # ------------------------------------------------------------------
    # READ — list by source system
    # ------------------------------------------------------------------
    async def get_by_source_system(
        self,
        source_system_id: int,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Customer], int]:
        """
        Fetch customers belonging to a specific CRM source system.

        Args:
            source_system_id: FK id of the source system.
            offset:           Number of records to skip.
            limit:            Max records to return.

        Returns:
            Tuple of (list of Customer ORM objects, total count).
        """
        count_query = (
            select(func.count())
            .select_from(Customer)
            .where(Customer.source_system_id == source_system_id)
        )
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = (
            _base_query()
            .where(Customer.source_system_id == source_system_id)
            .offset(offset)
            .limit(limit)
            .order_by(Customer.first_name.asc(), Customer.last_name.asc())
        )
        result = await self.db.execute(query)
        customers = list(result.scalars().unique().all())

        return customers, total

    # ------------------------------------------------------------------
    # READ — single by internal UUID
    # ------------------------------------------------------------------
    async def get_by_id(self, customer_id: uuid.UUID) -> Customer | None:
        """
        Fetch a single customer by internal UUID.

        Returns:
            Customer ORM object or None if not found.
        """
        query = _base_query().where(Customer.id == customer_id)
        result = await self.db.execute(query)
        return result.scalars().first()