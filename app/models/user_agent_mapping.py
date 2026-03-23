"""
Table user_agent_mappings
Links a dashboard user to their CRM agent identity.
Business rules:
  - One dashboard user can have at most ONE agent identity per CRM source
    → PK (user_id, source_system_id)
  - One CRM agent can be linked to at most ONE dashboard user
    → UNIQUE (agent_id, source_system_id)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class UserAgentMapping(Base):
    __tablename__ = "user_agent_mappings"
    __table_args__ = (
        # One CRM agent → at most one dashboard user
        UniqueConstraint(
            "agent_id",
            "source_system_id",
            name="uq_agent_source_one_user",
        ),
    )

    # ------------------------------------------------------------------
    # Surrogate primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Surrogate UUID primary key",
    )

    # ------------------------------------------------------------------
    # FK columns (together they enforce the composite business PK)
    # ------------------------------------------------------------------
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dashboard_users.id", ondelete="CASCADE"),
        nullable=False,
        comment="FK → dashboard_users",
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        comment="FK → agents",
    )
    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Audit timestamp
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the mapping was created",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    user: Mapped["DashboardUser"] = relationship(  # type: ignore[name-defined]
        "DashboardUser", back_populates="user_agent_mappings"
    )
    agent: Mapped["Agent"] = relationship(  # type: ignore[name-defined]
        "Agent", back_populates="user_agent_mappings"
    )
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="user_agent_mappings"
    )

    def __repr__(self) -> str:
        return (
            f"<UserAgentMapping id={self.id} user={self.user_id} "
            f"agent={self.agent_id} source={self.source_system_id}>"
        )
