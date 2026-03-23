"""
Pydantic schemas for the companies table.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class CompanyBase(BaseModel):
    company_name: str = Field(..., max_length=255, description="Company display name")
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = Field(default=None)


class CompanyCreate(CompanyBase):
    crm_company_id: str = Field(
        ...,
        max_length=50,
        description="Company ID as it exists in the source CRM",
    )
    source_system_id: int = Field(..., description="FK → source_systems")


class CompanyUpdate(BaseModel):
    company_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = Field(default=None)


class CompanyResponse(CompanyBase):
    id: UUID
    crm_company_id: str
    source_system: str          # e.g. "zammad" — from source_system.system_name

    model_config = {"from_attributes": True}


class CompanyBriefResponse(BaseModel):
    """Lightweight company info — used when nested inside other responses."""
    id: UUID
    company_name: str

    model_config = {"from_attributes": True}
