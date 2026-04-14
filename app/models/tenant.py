"""
Table  tenants
Each row represents one organisation (tenant) that uses the dashboard.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the tenant",
    )

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Formal name of the company using the dashboard",
    )
    slug: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="URL-friendly name e.g. acme-corp",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Status of the tenant's subscription",
    )

    contact_email: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
        comment="Primary contact email for the tenant",
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Record creation timestamp",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Last update timestamp",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    realm: Mapped["TenantRealm | None"] = relationship(  # type: ignore[name-defined]
        "TenantRealm", back_populates="tenant", uselist=False
    )
    dashboard_users: Mapped[list["DashboardUser"]] = relationship(  # type: ignore[name-defined]
        "DashboardUser", back_populates="tenant"
    )
    tenant_source_systems: Mapped[list["TenantSourceSystem"]] = relationship(  # type: ignore[name-defined]
        "TenantSourceSystem", back_populates="tenant"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r} active={self.is_active}>"
