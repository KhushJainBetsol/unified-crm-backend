"""
app/routes/customers.py  — UPDATED for multitenancy

Same pattern — get_current_user injected, tenant_id passed down.
Existing logic unchanged.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
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


@router.get("/", summary="List all customers for current tenant")
async def list_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    customers, total = await CustomerService(db).get_customers(
        page=page,
        page_size=page_size,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message="Customers fetched successfully",
    )


@router.get("/filter", summary="Filter customers")
async def filter_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    source: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    customers, total = await CustomerService(db).filter_customers(
        page=page,
        page_size=page_size,
        source=source,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return paginated(
        items=[_to_response(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
        message="Customers fetched successfully",
    )


@router.get("/{customer_id}", summary="Get customer by ID")
async def get_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),  # NEW
):
    tenant_id = current_user.require_tenant()               # NEW
    customer = await CustomerService(db).get_customer_or_404(
        customer_id,
        tenant_id=uuid.UUID(tenant_id),                     # NEW
    )
    return success("Customer fetched successfully", _to_response(customer))