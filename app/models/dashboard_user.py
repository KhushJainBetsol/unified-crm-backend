"""
Table  dashboard_users
Login accounts for staff who access your unified CRM dashboard.
These are NOT the same as CRM agents — see user_agent_mappings
for the link between a dashboard user and their CRM agent identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class DashboardUser(Base):
    __tablename__ = "dashboard_users"

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
    # Auth fields
    # ------------------------------------------------------------------
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        comment="Unique login email address",
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="argon2 hashed password",
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the account was created",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="When the account was last updated",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    user_source_systems: Mapped[list["UserSourceSystem"]] = relationship(  # type: ignore[name-defined]
        "UserSourceSystem", back_populates="user"
    )
    user_roles: Mapped[list["UserRole"]] = relationship(  # type: ignore[name-defined]
        "UserRole", back_populates="user"
    )
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
        "UserAgentMapping", back_populates="user"
    )
    deleted_tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket",
        foreign_keys="Ticket.deleted_by_id",
        back_populates="deleted_by",
    )

    def __repr__(self) -> str:
        return f"<DashboardUser id={self.id} email={self.email!r}>"
