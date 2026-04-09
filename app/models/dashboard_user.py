"""
Table  dashboard_users
Login accounts for staff who access the unified CRM dashboard.
Authentication is handled by Keycloak — no password stored here.
Role is cached from the JWT for fast permission checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class DashboardUser(Base):
    __tablename__ = "dashboard_users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'agent', 'superadmin')",
            name="chk_dashboard_users_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        comment="Internal dashboard UUID for the user",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, comment="The tenant this user belongs to",
    )
    keycloak_sub: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True,
        comment="The sub claim from Keycloak's JWT",
    )
    name: Mapped[str] = mapped_column(
    String(255), nullable=True, comment="Full name of the user",
    )
    email: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="User email address",
    )
    role: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Cached role: 'admin', 'agent', or 'superadmin'",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
        comment="Whether the user can log in",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="dashboard_users")  # type: ignore[name-defined]
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship("UserAgentMapping", back_populates="user")  # type: ignore[name-defined]
    deleted_tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", foreign_keys="Ticket.deleted_by_id", back_populates="deleted_by",
    )

    def __repr__(self) -> str:
        return f"<DashboardUser id={self.id} email={self.email!r} role={self.role!r}>"
