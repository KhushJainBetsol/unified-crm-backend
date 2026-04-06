"""
Table role_permissions
Maps permissions to roles.
Role is a plain VARCHAR string (Keycloak role name).
Composite PK (role, permission_id).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'agent', 'superadmin')",
            name="chk_role_permissions_role",
        ),
    )

    role: Mapped[str] = mapped_column(
        String(50), primary_key=True,
        comment="Keycloak role name: 'admin', 'agent', or 'superadmin'",
    )
    permission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True, comment="FK → permissions",
    )

    permission: Mapped["Permission"] = relationship("Permission", back_populates="role_permissions")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<RolePermission role={self.role!r} permission_id={self.permission_id}>"