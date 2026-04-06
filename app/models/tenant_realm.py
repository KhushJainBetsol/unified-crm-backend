"""
Table  tenant_realms
Maps each tenant to their Keycloak realm configuration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class TenantRealm(Base):
    __tablename__ = "tenant_realms"

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique ID for the realm config",
    )

    # ------------------------------------------------------------------
    # Foreign key
    # ------------------------------------------------------------------
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="Link to tenant (NULL = shared realm)",
    )

    # ------------------------------------------------------------------
    # Keycloak config
    # ------------------------------------------------------------------
    realm_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="The Keycloak realm identifier",
    )
    issuer_url: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="The OpenID Connect issuer URL",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this realm is currently usable",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    tenant: Mapped["Tenant | None"] = relationship(  # type: ignore[name-defined]
        "Tenant", back_populates="realm"
    )

    def __repr__(self) -> str:
        return f"<TenantRealm id={self.id} realm={self.realm_name!r} active={self.is_active}>"
