"""
Table  tenant_source_systems
Maps each tenant to the CRM source systems they are connected to.
Replaces the old user_source_systems table — CRM connections are now
scoped at the tenant level, not the individual user level.
Composite PK (tenant_id, source_system_id).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class TenantSourceSystem(Base):
    __tablename__ = "tenant_source_systems"

    # Composite primary key
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )

    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # ✅ Nullable FK (NOT part of PK)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_integrations.id", ondelete="SET NULL"),  # ✅ correct table name
        nullable=True,
        default=None,
        comment="FK → crm_integrations (specific CRM integration instance)",
    )
# Remove primary_key=True — it's nullable, can't be part of PK

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    crm_org_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="tenant_source_systems",
    )

    source_system: Mapped["SourceSystem"] = relationship(
        "SourceSystem",
        back_populates="tenant_source_systems",
    )

    integration: Mapped["CrmIntegration"] = relationship(
        "CrmIntegration",
        back_populates="tenant_source_systems",
    )




    def __repr__(self) -> str:
        return (
            f"<TenantSourceSystem tenant_id={self.tenant_id} "
            f"source_system_id={self.source_system_id} "
            f"active={self.is_active} "
            f"crm_org_id={self.crm_org_id!r} "
            f"integration_id={self.integration_id!r}>"  # ✅ fixed duplicate
        )