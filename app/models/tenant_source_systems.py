# """
# Table  tenant_source_systems
# Maps each tenant to the CRM source systems they are connected to.
# Replaces the old user_source_systems table — CRM connections are now
# scoped at the tenant level, not the individual user level.
# Composite PK (tenant_id, source_system_id).
# """

# from __future__ import annotations

# import uuid

# from sqlalchemy import Boolean, ForeignKey, Integer
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class TenantSourceSystem(Base):
#     __tablename__ = "tenant_source_systems"

#     # ------------------------------------------------------------------
#     # Composite primary key
#     # ------------------------------------------------------------------
#     tenant_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("tenants.id", ondelete="CASCADE"),
#         primary_key=True,
#         comment="FK → tenants",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         Integer,
#         ForeignKey("source_systems.id", ondelete="CASCADE"),
#         primary_key=True,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Status
#     # ------------------------------------------------------------------
#     is_active: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=True,
#         comment="Status of the CRM integration for this tenant",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
#         "Tenant", back_populates="tenant_source_systems"
#     )
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="tenant_source_systems"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<TenantSourceSystem tenant_id={self.tenant_id} "
#             f"source_system_id={self.source_system_id} active={self.is_active}>"
#         )

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

    # ------------------------------------------------------------------
    # Composite primary key
    # ------------------------------------------------------------------
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → tenants",
    )
    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Status of the CRM integration for this tenant",
    )

    # ------------------------------------------------------------------
    # CRM organisation identifier
    # Populated at tenant-creation time by calling the CRM's own API.
    # NULL means the lookup has not succeeded yet (CRM unreachable, org
    # not found, etc.) — it does NOT block tenant creation.
    # ------------------------------------------------------------------
    crm_org_id: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        default=None,
        comment=(
            "Organisation/Account ID returned by the external CRM "
            "(Zammad organization id or EspoCRM Account id). "
            "NULL until successfully fetched."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
        "Tenant", back_populates="tenant_source_systems"
    )
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="tenant_source_systems"
    )

    def __repr__(self) -> str:
        return (
            f"<TenantSourceSystem tenant_id={self.tenant_id} "
            f"source_system_id={self.source_system_id} "
            f"active={self.is_active} crm_org_id={self.crm_org_id!r}>"
        )