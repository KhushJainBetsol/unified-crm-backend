"""
Table  user_roles
Junction table: maps dashboard users to their assigned roles.
Composite PK (user_id, role_id).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class UserRole(Base):
    __tablename__ = "user_roles"

    # ------------------------------------------------------------------
    # Composite primary key
    # ------------------------------------------------------------------
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dashboard_users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → dashboard_users",
    )
    role_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → roles",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    user: Mapped["DashboardUser"] = relationship(  # type: ignore[name-defined]
        "DashboardUser", back_populates="user_roles"
    )
    role: Mapped["Role"] = relationship(  # type: ignore[name-defined]
        "Role", back_populates="user_roles"
    )

    def __repr__(self) -> str:
        return f"<UserRole user_id={self.user_id} role_id={self.role_id}>"
