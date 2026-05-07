"""
app/models/dashboard_user.py

Table  dashboard_users
Login accounts for staff who access the unified CRM dashboard.

Authentication is handled by Keycloak — no password is stored here.
``keycloak_sub`` is the user's UUID in Keycloak and is the key used for
all Admin REST API calls (update, delete, disable).

Soft-delete strategy
--------------------
Admins and agents are never hard-deleted from this table because tickets,
comments, and audit records carry a FK to ``dashboard_users.id``.
Removing a row would either orphan those records or cascade-delete them,
both of which are undesirable.

Instead:
  - ``is_active = False``  →  user cannot log in (Keycloak account is also
                              disabled/deleted by the service layer)
  - ``deleted_at``         →  populated when the account is deactivated,
                              so queries can filter on recency if needed

The one exception is the CASCADE from ``tenants.id``: when a tenant is
hard-deleted, Postgres cascades and removes all of its users from this
table.  The service layer captures ``keycloak_sub`` values *before* that
commit so it can clean them up from Keycloak afterwards.
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

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Internal dashboard UUID — used as FK target by tickets, comments, etc.",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this user belongs to.  CASCADE means all users are "
                "removed when the tenant is hard-deleted.",
    )
    keycloak_sub: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        unique=True,
        comment="Keycloak user UUID (the 'sub' claim).  Used to target this "
                "user via the Keycloak Admin REST API (update / delete / disable).",
    )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Full name — cached from Keycloak, updated on profile change.",
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Email address — also the Keycloak username in this setup.",
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Cached role from JWT: 'admin', 'agent', or 'superadmin'.",
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="False once the account is deactivated.  Keycloak account is "
                "simultaneously disabled or deleted by the service layer.",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="Populated when is_active is set to False.  NULL means the "
                "account is still live.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
        "Tenant",
        back_populates="dashboard_users",
    )
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
        "UserAgentMapping",
        back_populates="user",
    )
    deleted_tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket",
        foreign_keys="Ticket.deleted_by_id",
        back_populates="deleted_by",
    )

    def __repr__(self) -> str:
        return (
            f"<DashboardUser id={self.id} email={self.email!r} "
            f"role={self.role!r} active={self.is_active}>"
        )