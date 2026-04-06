# """
# Pydantic schemas for the customers table.
# """

# from __future__ import annotations

# from uuid import UUID

# from pydantic import BaseModel, EmailStr, Field


# class CustomerBase(BaseModel):
#     first_name: str = Field(..., max_length=100, description="Customer first name")
#     last_name: str | None = Field(default=None, max_length=100)
#     email: EmailStr | None = Field(default=None)
#     phone: str | None = Field(default=None, max_length=50)


# class CustomerCreate(CustomerBase):
#     crm_customer_id: str = Field(
#         ...,
#         max_length=50,
#         description="Customer ID as it exists in the source CRM",
#     )
#     source_system_id: int = Field(..., description="FK → source_systems")
#     company_id: UUID | None = Field(default=None, description="FK → companies (optional)")


# class CustomerUpdate(BaseModel):
#     first_name: str | None = Field(default=None, max_length=100)
#     last_name: str | None = Field(default=None, max_length=100)
#     email: EmailStr | None = Field(default=None)
#     phone: str | None = Field(default=None, max_length=50)
#     company_id: UUID | None = Field(default=None)


# class CustomerResponse(CustomerBase):
#     id: UUID
#     crm_customer_id: str
#     source_system: str          # e.g. "zammad" — from source_system.system_name
#     company_id: UUID | None

#     model_config = {"from_attributes": True}


# class CustomerBriefResponse(BaseModel):
#     """Lightweight customer info — used when nested inside ticket responses."""
#     id: UUID
#     first_name: str
#     last_name: str | None
#     email: EmailStr | None

#     model_config = {"from_attributes": True}
from __future__ import annotations
from uuid import UUID
from pydantic import BaseModel, Field


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
    source_system_id: int

    model_config = {"from_attributes": True}