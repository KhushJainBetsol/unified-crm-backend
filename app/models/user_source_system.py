"""
Table  user_source_systems
Junction table: which CRM source systems a dashboard user has access to.
Composite PK (user_id, source_system_id) — a user can belong to one or
multiple CRM systems.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class UserSourceSystem(Base):
    __tablename__ = "user_source_systems"

    # ------------------------------------------------------------------
    # Composite primary key
    # ------------------------------------------------------------------
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dashboard_users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → dashboard_users",
    )
    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="CASCADE"),
        primary_key=True,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    user: Mapped["DashboardUser"] = relationship(  # type: ignore[name-defined]
        "DashboardUser", back_populates="user_source_systems"
    )
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="user_source_systems"
    )

    def __repr__(self) -> str:
        return (
            f"<UserSourceSystem user_id={self.user_id} "
            f"source_system_id={self.source_system_id}>"
        )
