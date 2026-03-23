"""
Table  agents
CRM support agents who are assigned to / work tickets in the external CRM.
Not the same as dashboard_users — a CRM agent may or may not have a
dashboard login (see user_agent_mappings for that link).
Each (crm_agent_id, source_system_id) pair must be unique.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint(
            "crm_agent_id",
            "source_system_id",
            name="uq_agent_crm_source",
        ),
    )

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Internal UUID primary key",
    )

    # ------------------------------------------------------------------
    # CRM reference
    # ------------------------------------------------------------------
    crm_agent_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Agent ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Agent display name",
    )
    email: Mapped[str|None] = mapped_column(
        String(255),
        nullable=True,
        default=None,
        comment="CRM may not always expose agent email",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether the agent is currently active",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="agents"
    )
    tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="agent"
    )
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
        "UserAgentMapping", back_populates="agent"
    )

    def __repr__(self) -> str:
        return (
            f"<Agent id={self.id} name={self.name!r} "
            f"active={self.is_active} crm_id={self.crm_agent_id!r}>"
        )
