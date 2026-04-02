# """
# Table  companies
# Stores CRM companies/accounts synced from external CRM systems.
# Each (crm_company_id, source_system_id) pair must be unique to prevent
# duplicate accounts being imported from the same CRM.
# """

# from __future__ import annotations

# import uuid

# from sqlalchemy import ForeignKey, String, UniqueConstraint
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Company(Base):
#     __tablename__ = "companies"
#     __table_args__ = (
#         UniqueConstraint(
#             "crm_company_id",
#             "source_system_id",
#             name="uq_company_crm_source",
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
#     crm_company_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Company ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Attributes
#     # ------------------------------------------------------------------
#     company_name: Mapped[str] = mapped_column(
#         String(255),
#         nullable=False,
#         comment="Company display name",
#     )
#     phone: Mapped[str | None] = mapped_column(
#         String(50),
#         nullable=True,
#         default=None,
#         comment="Optional phone number",
#     )
#     email: Mapped[str | None] = mapped_column(
#         String(255),
#         nullable=True,
#         default=None,
#         comment="Optional email address",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="companies"
#     )
#     customers: Mapped[list["Customer"]] = relationship(  # type: ignore[name-defined]
#         "Customer", back_populates="company"
#     )
#     tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
#         "Ticket", back_populates="company"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Company id={self.id} name={self.company_name!r} "
#             f"crm_id={self.crm_company_id!r}>"
#         )

"""
Table  companies
Stores CRM companies/accounts synced from external CRM systems.
Each (tenant_id, crm_company_id, source_system_id) must be unique.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "crm_company_id",
            "source_system_id",
            name="uq_company_tenant_crm_source",
        ),
    )

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Internal UUID primary key",
    )

    # ------------------------------------------------------------------
    # Tenant
    # ------------------------------------------------------------------
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="FK → tenants",
    )

    # ------------------------------------------------------------------
    # CRM reference
    # ------------------------------------------------------------------
    crm_company_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Company ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    company_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Company display name",
    )
    phone: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        default=None,
        comment="Optional phone number",
    )
    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        comment="Optional email address",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
        "Tenant", lazy="noload"
    )
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="companies"
    )
    customers: Mapped[list["Customer"]] = relationship(  # type: ignore[name-defined]
        "Customer", back_populates="company"
    )
    tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="company"
    )

    def __repr__(self) -> str:
        return (
            f"<Company id={self.id} name={self.company_name!r} "
            f"crm_id={self.crm_company_id!r}>"
        )

