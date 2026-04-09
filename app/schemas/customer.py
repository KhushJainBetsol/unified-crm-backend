"""
app/schemas/customer.py

Pydantic schemas for the customers table.

NOTE: The Customer model merged first_name + last_name into a single
`name` field. All schemas here reflect that — there is no first_name
or last_name anywhere. CustomerBriefResponse (used nested inside ticket
responses) has also been updated accordingly.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class CustomerCreate(BaseModel):
    name: str = Field(..., max_length=200, description="Customer full name")
    email: str | None = Field(default=None)
    phone: str | None = Field(default=None)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None)
    phone: str | None = Field(default=None)


class CustomerResponse(BaseModel):
    id: UUID
    crm_customer_id: str
    source_system: str          # e.g. "zammad" — from source_system.system_name
    name: str                   # merged full name — no first_name / last_name
    email: str | None
    phone: str | None

    model_config = {"from_attributes": True}


class CustomerBriefResponse(BaseModel):
    """Lightweight customer info — used when nested inside ticket responses."""
    id: UUID
    name: str                   # merged full name — no first_name / last_name
    email: EmailStr | None

    model_config = {"from_attributes": True}