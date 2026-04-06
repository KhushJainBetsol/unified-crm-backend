from __future__ import annotations
from uuid import UUID
from pydantic import BaseModel, Field, EmailStr


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
    name: str
    email: str | None
    phone: str | None
    crm_customer_id: str
    source_system: str

    model_config = {"from_attributes": True}

class CustomerBriefResponse(BaseModel):
    """Lightweight customer info — used when nested inside ticket responses."""
    id: UUID
    name: str
    email: EmailStr | None

    model_config = {"from_attributes": True}