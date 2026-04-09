"""
app/services/company_service.py

Business logic for companies — sits between routes and repositories.

Multitenancy:
  - Every public method accepts tenant_id: uuid.UUID | None.
  - It is passed down to the repository on every call.
  - Never skip it in a multitenant request.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.source_system import SourceSystem
from app.repositories.company_repository import CompanyRepository

logger = logging.getLogger(__name__)


class CompanyService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CompanyRepository(db)

    async def _resolve_source_system(self, source: str) -> SourceSystem:
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

    async def get_companies(
        self,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[list, int]:
        offset = (page - 1) * page_size
        return await self.repo.get_all(
            tenant_id=tenant_id,
            offset=offset,
            limit=page_size,
        )

    async def filter_companies(
        self,
        page: int,
        page_size: int,
        tenant_id: uuid.UUID | None = None,
        source: str | None = None,
    ) -> tuple[list, int]:
        offset = (page - 1) * page_size

        if source:
            source_obj = await self._resolve_source_system(source)
            return await self.repo.get_by_source_system(
                source_system_id=source_obj.id,
                tenant_id=tenant_id,
                offset=offset,
                limit=page_size,
            )

        return await self.repo.get_all(
            tenant_id=tenant_id,
            offset=offset,
            limit=page_size,
        )

    async def get_company_or_404(
        self,
        company_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
    ) -> Company:
        """
        Fetch a single company or raise HTTP 404.
        Passing tenant_id ensures a company from another tenant returns 404
        rather than leaking data.
        """
        company = await self.repo.get_by_id(company_id, tenant_id=tenant_id)
        if not company:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Company {company_id} not found",
            )
        return company