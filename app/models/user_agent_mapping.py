"""
Table user_agent_mappings
Links a dashboard user to their CRM agent identity.
Unique constraint: (dashboard_user_id, agent_id)
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class UserAgentMapping(Base):
    __tablename__ = "user_agent_mappings"
    __table_args__ = (
        UniqueConstraint(
            "dashboard_user_id", "agent_id",
            name="uq_user_agent_mapping",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    dashboard_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dashboard_users.id", ondelete="CASCADE"),
        nullable=False, comment="FK → dashboard_users",
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, comment="FK → agents",
    )
    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False, comment="FK → source_systems",
    )

    user: Mapped["DashboardUser"] = relationship(  # type: ignore[name-defined]
        "DashboardUser", foreign_keys=[dashboard_user_id], back_populates="user_agent_mappings",
    )
    agent: Mapped["Agent"] = relationship("Agent", back_populates="user_agent_mappings")  # type: ignore[name-defined]
    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="user_agent_mappings")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return (
            f"<UserAgentMapping id={self.id} "
            f"dashboard_user={self.dashboard_user_id} agent={self.agent_id}>"
        )