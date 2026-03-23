"""
app/routes/companies.py

GET /companies/                        → paginated list   (CompanyResponse)
GET /companies/source/{source_system}  → filtered by CRM  (CompanyResponse)
GET /companies/{id}                    → full detail       (CompanyResponse)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.source_system import SourceSystem
from app.repositories.company_repository import CompanyRepository
from app.schemas.company import CompanyResponse
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companies", tags=["Companies"])


# ---------------------------------------------------------------------------
# Mapper — ORM object → Pydantic schema dict
# ---------------------------------------------------------------------------

def _to_response(company) -> dict:
    return CompanyResponse(
        id=company.id,
        crm_company_id=company.crm_company_id,
        source_system=company.source_system.system_name,
        company_name=company.company_name,
        phone=company.phone,
        email=company.email,
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
# GET /companies/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all companies")
async def list_companies(
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    companies, total = await CompanyRepository(db).get_all(
        offset=offset,
        limit=page_size,
    )
    logger.debug("list_companies: returned %d of %d total", len(companies), total)
    return paginated(
        items=[_to_response(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
        message="Companies fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /companies/source/{source_system_name}
# IMPORTANT: must be defined BEFORE /{company_id} so FastAPI does not
# try to parse the literal string "source" as a UUID
# ---------------------------------------------------------------------------

@router.get("/source/{source_system_name}", summary="List companies by CRM source")
async def list_companies_by_source(
    source_system_name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    source = await _get_source_system(source_system_name, db)

    if not source:
        logger.warning("list_companies_by_source: unknown source '%s'", source_system_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source system '{source_system_name}' not found. Valid values: zammad, espocrm",
        )

    offset = (page - 1) * page_size
    companies, total = await CompanyRepository(db).get_by_source_system(
        source_system_id=source.id,
        offset=offset,
        limit=page_size,
    )
    logger.debug(
        "list_companies_by_source: source=%s returned %d of %d",
        source_system_name, len(companies), total,
    )
    return paginated(
        items=[_to_response(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Companies for '{source_system_name}' fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /companies/{company_id}
# ---------------------------------------------------------------------------

@router.get("/{company_id}", summary="Get company by ID")
async def get_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    company = await CompanyRepository(db).get_by_id(company_id)

    if not company:
        logger.warning("get_company: company %s not found", company_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company {company_id} not found",
        )

    return success("Company fetched successfully", _to_response(company))