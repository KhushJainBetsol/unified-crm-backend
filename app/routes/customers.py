"""
app/routes/customers.py

GET /customers/        → paginated list
GET /customers/filter  → filtered list (?source)
GET /customers/{id}    → full detail
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.customer import CustomerResponse
from app.services.customer_service import CustomerService
from app.utils.response import paginated, success

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["Customers"])


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
# GET /customers/
# ---------------------------------------------------------------------------

@router.get("/", summary="List all customers")
async def list_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    customers, total = await CustomerService(db).get_customers(
        page=page,
        page_size=page_size,
    )
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message="Customers fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /customers/filter
# NOTE: defined before /{customer_id} so "filter" is not parsed as a UUID
# ---------------------------------------------------------------------------

@router.get("/filter", summary="Filter customers by source system")
async def filter_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    source: str | None = Query(default=None, description="CRM source: zammad, espocrm"),
    db: AsyncSession = Depends(get_db),
):
    customers, total = await CustomerService(db).filter_customers(
        page=page,
        page_size=page_size,
        source=source,
    )
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message="Customers fetched successfully",
    )


# ---------------------------------------------------------------------------
# GET /customers/{customer_id}
# ---------------------------------------------------------------------------

@router.get("/{customer_id}", summary="Get customer by ID")
async def get_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    customer = await CustomerService(db).get_customer_or_404(customer_id)
    return success("Customer fetched successfully", _to_response(customer))