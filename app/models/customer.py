# """
# Table  customers (Ticket Requesters)
# Stores contacts / end-users who raise support tickets in a CRM.
# Each (crm_customer_id, source_system_id) pair must be unique.
# """

# from __future__ import annotations

# import uuid

# from sqlalchemy import ForeignKey, String, UniqueConstraint
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Customer(Base):
#     __tablename__ = "customers"
#     __table_args__ = (
#         UniqueConstraint(
#             "crm_customer_id",
#             "source_system_id",
#             name="uq_customer_crm_source",
#         ),
#     )

#     # ------------------------------------------------------------------
#     # Primary key
#     # ------------------------------------------------------------------
#     id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         primary_key=True,
#         default=uuid.uuid4,
#         comment="Internal UUID primary key",
#     )

#     # ------------------------------------------------------------------
#     # CRM reference
#     # ------------------------------------------------------------------
#     crm_customer_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Customer ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Optional company link
#     # ------------------------------------------------------------------
#     company_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("companies.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#         comment="FK → companies (optional)",
#     )

#     # ------------------------------------------------------------------
#     # Attributes
#     # ------------------------------------------------------------------
#     first_name: Mapped[str] = mapped_column(
#         String(100),
#         nullable=False,
#         comment="Customer first name",
#     )
#     last_name: Mapped[str | None] = mapped_column(
#         String(100),
#         nullable=True,
#         default=None,
#         comment="Customer last name (optional)",
#     )
#     email: Mapped[str | None] = mapped_column(
#         String(255),
#         nullable=True,
#         default=None,
#         comment="Optional email address",
#     )
#     phone: Mapped[str | None] = mapped_column(
#         String(50),
#         nullable=True,
#         default=None,
#         comment="Optional phone number",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="customers"
#     )
#     company: Mapped["Company | None"] = relationship(  # type: ignore[name-defined]
#         "Company", back_populates="customers"
#     )
#     tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
#         "Ticket", back_populates="customer"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Customer id={self.id} name={self.first_name!r} {self.last_name!r} "
#             f"crm_id={self.crm_customer_id!r}>"
#         )

"""
Table  customers (Ticket Requesters)
first_name + last_name merged into single 'name' field.
tenant_id is nullable — isolation logic not yet decided.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "crm_customer_id", "source_system_id",
            name="uq_customer_tenant_crm_source",
        ),
        Index("idx_customers_tenant", "tenant_id"),
        Index("idx_customers_company", "company_id"),
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
    crm_customer_id: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Customer ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"), nullable=False,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True, default=None,
    )

    # first_name + last_name merged into single name field
    name: Mapped[str] = mapped_column(
        String(200), nullable=False,
        comment="Full name of the customer",
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    tenant: Mapped["Tenant | None"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]
    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="customers")  # type: ignore[name-defined]
    company: Mapped["Company | None"] = relationship("Company", back_populates="customers")  # type: ignore[name-defined]
    tickets: Mapped[list["Ticket"]] = relationship("Ticket", back_populates="customer")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Customer id={self.id} name={self.name!r} crm_id={self.crm_customer_id!r}>"