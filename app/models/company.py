"""
Table  companies
Stores CRM companies/accounts synced from external CRM systems.
tenant_id is nullable — isolation logic not yet decided.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "crm_company_id", "source_system_id",
            name="uq_company_tenant_crm_source",
        ),
        Index("idx_companies_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="FK → tenants — nullable until isolation logic is finalised",
    )
    crm_company_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="Company ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    tenant: Mapped["Tenant | None"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]
    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="companies")  # type: ignore[name-defined]
    customers: Mapped[list["Customer"]] = relationship("Customer", back_populates="company")  # type: ignore[name-defined]
    tickets: Mapped[list["Ticket"]] = relationship("Ticket", back_populates="company")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.company_name!r} crm_id={self.crm_company_id!r}>"