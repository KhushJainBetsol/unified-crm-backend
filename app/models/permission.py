# """
# Table  permissions
# Stores granular system permissions assigned to roles.
# Example values: 'ticket.read', 'ticket.update', 'ticket.delete'
# """

# from __future__ import annotations

# from sqlalchemy import Integer, String
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Permission(Base):
#     __tablename__ = "permissions"

#     id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
#     permission_name: Mapped[str] = mapped_column(
#         String(100),
#         nullable=False,
#         unique=True,
#         comment="Unique permission key, e.g. ticket.read / ticket.update / ticket.delete",
#     )

#     role_permissions: Mapped[list["RolePermission"]] = relationship(  # type: ignore[name-defined]
#         "RolePermission", back_populates="permission"
#     )

#     def __repr__(self) -> str:
#         return f"<Permission id={self.id} name={self.permission_name!r}>"

"""
Table  permissions
Stores granular system permissions assigned to roles.
Example values: 'tickets:delete', 'tickets:view', 'users:invite'
Grouped by module for easier management.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="String identifier e.g. tickets:delete",
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        comment="Human-readable explanation of the permission",
    )
    module: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Grouping e.g. 'tickets', 'users'",
    )

    role_permissions: Mapped[list["RolePermission"]] = relationship(  # type: ignore[name-defined]
        "RolePermission", back_populates="permission"
    )

    def __repr__(self) -> str:
        return f"<Permission id={self.id} name={self.name!r} module={self.module!r}>"

