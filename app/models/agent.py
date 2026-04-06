"""
Table  agents
CRM support agents synced from external CRM systems.
tenant_id is nullable — isolation logic not yet decided.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "crm_agent_id", "source_system_id",
            name="uq_agent_tenant_crm_source",
        ),
        Index("idx_agents_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="FK → tenants — nullable until isolation logic is finalised",
    )
    crm_agent_id: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Agent ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    tenant: Mapped["Tenant | None"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]
    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="agents")  # type: ignore[name-defined]
    tickets: Mapped[list["Ticket"]] = relationship("Ticket", back_populates="agent")  # type: ignore[name-defined]
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship("UserAgentMapping", back_populates="agent")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Agent id={self.id} name={self.name!r} crm_id={self.crm_agent_id!r}>"