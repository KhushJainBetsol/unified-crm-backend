"""
app/routes/companies.py  — UPDATED for multitenancy

Same pattern as tickets/customers — get_current_user injected,
tenant_id passed down. Existing logic unchanged.

GET /companies/        → paginated list  (tenant-scoped)
GET /companies/filter  → filtered list   (tenant-scoped, ?source)
GET /companies/{id}    → full detail     (tenant-scoped)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.dependencies import get_db
from app.schemas.company import CompanyResponse
from app.services.company_service import CompanyService
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companies", tags=["Companies"])


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
# GET /companies/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all companies for current tenant")
async def list_companies(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # NEW
):
    tenant_id = current_user.require_tenant()                  # NEW
    companies, total = await CompanyService(db).get_companies(
        page=page,
        page_size=page_size,
        tenant_id=uuid.UUID(tenant_id),                        # NEW
    )
    return paginated(
        items=[_to_response(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
        message="Companies fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /companies/filter
# NOTE: defined before /{company_id} so "filter" is not parsed as a UUID
# ---------------------------------------------------------------------------

@router.get("/filter", summary="Filter companies by source system")
async def filter_companies(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    source: str | None = Query(default=None, description="CRM source: zammad, espocrm"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # NEW
):
    tenant_id = current_user.require_tenant()                  # NEW
    companies, total = await CompanyService(db).filter_companies(
        page=page,
        page_size=page_size,
        source=source,
        tenant_id=uuid.UUID(tenant_id),                        # NEW
    )
    return paginated(
        items=[_to_response(c) for c in companies],
        total=total,
        page=page,
        page_size=page_size,
        message="Companies fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /companies/{company_id}
# ---------------------------------------------------------------------------

@router.get("/{company_id}", summary="Get company by ID")
async def get_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),   # NEW
):
    tenant_id = current_user.require_tenant()                  # NEW
    company = await CompanyService(db).get_company_or_404(
        company_id,
        tenant_id=uuid.UUID(tenant_id),                        # NEW
    )
    return success("Company fetched successfully", _to_response(company))