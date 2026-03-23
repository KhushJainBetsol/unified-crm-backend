"""
Table role_permissions
Junction table: maps permissions to roles.
Composite PK (role_id, permission_id).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class RolePermission(Base):
    __tablename__ = "role_permissions"

    # ------------------------------------------------------------------
    # Composite primary key
    # ------------------------------------------------------------------
    role_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → roles",
    )
    permission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → permissions",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    role: Mapped["Role"] = relationship(  # type: ignore[name-defined]
        "Role", back_populates="role_permissions"
    )
    permission: Mapped["Permission"] = relationship(  # type: ignore[name-defined]
        "Permission", back_populates="role_permissions"
    )

    def __repr__(self) -> str:
        return (
            f"<RolePermission role_id={self.role_id} "
            f"permission_id={self.permission_id}>"
        )
