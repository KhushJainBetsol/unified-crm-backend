# """
# Table role_permissions
# Junction table: maps permissions to roles.
# Composite PK (role_id, permission_id).
# """

# from __future__ import annotations

# from sqlalchemy import ForeignKey, Integer
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class RolePermission(Base):
#     __tablename__ = "role_permissions"

#     # ------------------------------------------------------------------
#     # Composite primary key
#     # ------------------------------------------------------------------
#     role_id: Mapped[int] = mapped_column(
#         Integer,
#         ForeignKey("roles.id", ondelete="CASCADE"),
#         primary_key=True,
#         comment="FK → roles",
#     )
#     permission_id: Mapped[int] = mapped_column(
#         Integer,
#         ForeignKey("permissions.id", ondelete="CASCADE"),
#         primary_key=True,
#         comment="FK → permissions",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     role: Mapped["Role"] = relationship(  # type: ignore[name-defined]
#         "Role", back_populates="role_permissions"
#     )
#     permission: Mapped["Permission"] = relationship(  # type: ignore[name-defined]
#         "Permission", back_populates="role_permissions"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<RolePermission role_id={self.role_id} "
#             f"permission_id={self.permission_id}>"
#         )

# """
# Table role_permissions
# Junction table: maps permissions to roles.
# Role is stored as a plain VARCHAR string (matching Keycloak role names)
# instead of a FK to a roles table — roles are managed in Keycloak.
# Composite PK (role, permission_id).
# """

# from __future__ import annotations

# from sqlalchemy import ForeignKey, Integer, String
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class RolePermission(Base):
#     __tablename__ = "role_permissions"

#     # ------------------------------------------------------------------
#     # Composite primary key
#     # ------------------------------------------------------------------
#     role: Mapped[str] = mapped_column(
#         String(50),
#         primary_key=True,
#         comment="Keycloak role name e.g. 'admin', 'agent'",
#     )
#     permission_id: Mapped[int] = mapped_column(
#         Integer,
#         ForeignKey("permissions.id", ondelete="CASCADE"),
#         primary_key=True,
#         comment="FK → permissions",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     permission: Mapped["Permission"] = relationship(  # type: ignore[name-defined]
#         "Permission", back_populates="role_permissions"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<RolePermission role={self.role!r} "
#             f"permission_id={self.permission_id}>"
#         )

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