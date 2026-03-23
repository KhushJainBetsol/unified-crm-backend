"""
Table  roles
Defines system roles assigned to dashboard users.
Seeded values: 'Admin', 'Agent'
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        comment="Unique role name, e.g. Admin / Agent",
    )

    user_roles: Mapped[list["UserRole"]] = relationship(  # type: ignore[name-defined]
        "UserRole", back_populates="role"
    )
    role_permissions: Mapped[list["RolePermission"]] = relationship(  # type: ignore[name-defined]
        "RolePermission", back_populates="role"
    )

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.role_name!r}>"
