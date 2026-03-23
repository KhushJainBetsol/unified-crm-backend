"""
app/routes/customers.py

GET /customers/                        → paginated list   (CustomerResponse)
GET /customers/source/{source_system}  → filtered by CRM  (CustomerResponse)
GET /customers/{id}                    → full detail       (CustomerResponse)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.source_system import SourceSystem
from app.repositories.customer_repository import CustomerRepository
from app.schemas.customer import CustomerResponse
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["Customers"])


# ---------------------------------------------------------------------------
# Mapper — ORM object → Pydantic schema dict
# ---------------------------------------------------------------------------

def _to_response(customer) -> dict:
    return CustomerResponse(
        id=customer.id,
        crm_customer_id=customer.crm_customer_id,
        source_system=customer.source_system.system_name,
        first_name=customer.first_name,
        last_name=customer.last_name,
        email=customer.email,
        phone=customer.phone,
        company_id=customer.company_id,
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
# GET /customers/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all customers")
async def list_customers(
    page: int = Query(default=1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page (max 100)"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    customers, total = await CustomerRepository(db).get_all(
        offset=offset,
        limit=page_size,
    )
    logger.debug("list_customers: returned %d of %d total", len(customers), total)
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message="Customers fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /customers/source/{source_system_name}
# IMPORTANT: must be defined BEFORE /{customer_id} so FastAPI does not
# try to parse the literal string "source" as a UUID
# ---------------------------------------------------------------------------

@router.get("/source/{source_system_name}", summary="List customers by CRM source")
async def list_customers_by_source(
    source_system_name: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    source = await _get_source_system(source_system_name, db)

    if not source:
        logger.warning("list_customers_by_source: unknown source '%s'", source_system_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source system '{source_system_name}' not found. Valid values: zammad, espocrm",
        )

    offset = (page - 1) * page_size
    customers, total = await CustomerRepository(db).get_by_source_system(
        source_system_id=source.id,
        offset=offset,
        limit=page_size,
    )
    logger.debug(
        "list_customers_by_source: source=%s returned %d of %d",
        source_system_name, len(customers), total,
    )
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message=f"Customers for '{source_system_name}' fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /customers/{customer_id}
# ---------------------------------------------------------------------------

@router.get("/{customer_id}", summary="Get customer by ID")
async def get_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    customer = await CustomerRepository(db).get_by_id(customer_id)

    if not customer:
        logger.warning("get_customer: customer %s not found", customer_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer {customer_id} not found",
        )

    return success("Customer fetched successfully", _to_response(customer))