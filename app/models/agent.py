# """
# Table  agents
# CRM support agents who are assigned to / work tickets in the external CRM.
# Not the same as dashboard_users — a CRM agent may or may not have a
# dashboard login (see user_agent_mappings for that link).
# Each (crm_agent_id, source_system_id) pair must be unique.
# """

# from __future__ import annotations

# import uuid

# from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Agent(Base):
#     __tablename__ = "agents"
#     __table_args__ = (
#         UniqueConstraint(
#             "crm_agent_id",
#             "source_system_id",
#             name="uq_agent_crm_source",
#         ),
#     )

#     # ------------------------------------------------------------------
#     # Primary key
#     # ------------------------------------------------------------------
#     id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         primary_key=True,
#         default=uuid.uuid4,
#         comment="Internal UUID primary key",
#     )

#     # ------------------------------------------------------------------
#     # CRM reference
#     # ------------------------------------------------------------------
#     crm_agent_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Agent ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Attributes
#     # ------------------------------------------------------------------
#     name: Mapped[str] = mapped_column(
#         String(200),
#         nullable=False,
#         comment="Agent display name",
#     )
#     email: Mapped[str|None] = mapped_column(
#         String(255),
#         nullable=True,
#         default=None,
#         comment="CRM may not always expose agent email",
#     )
#     is_active: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=True,
#         comment="Whether the agent is currently active",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="agents"
#     )
#     tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
#         "Ticket", back_populates="agent"
#     )
#     user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
#         "UserAgentMapping", back_populates="agent"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Agent id={self.id} name={self.name!r} "
#             f"active={self.is_active} crm_id={self.crm_agent_id!r}>"
#         )

# """
# Table  agents
# CRM support agents who are assigned to / work tickets in the external CRM.
# Not the same as dashboard_users — a CRM agent may or may not have a
# dashboard login (see user_agent_mappings for that link).
# Each (tenant_id, crm_agent_id, source_system_id) must be unique.
# """

# from __future__ import annotations

# import uuid

# from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Agent(Base):
#     __tablename__ = "agents"
#     __table_args__ = (
#         UniqueConstraint(
#             "tenant_id",
#             "crm_agent_id",
#             "source_system_id",
#             name="uq_agent_tenant_crm_source",
#         ),
#     )

#     # ------------------------------------------------------------------
#     # Primary key
#     # ------------------------------------------------------------------
#     id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         primary_key=True,
#         default=uuid.uuid4,
#         comment="Internal UUID primary key",
#     )

#     # ------------------------------------------------------------------
#     # Tenant — nullable because agents are not directly tied to a tenant
#     # in the CRM. Resolved later via their ticket's company.
#     # ------------------------------------------------------------------
#     tenant_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("tenants.id", ondelete="CASCADE"),
#         nullable=True,
#         index=True,
#         comment="FK → tenants (resolved from company during ticket sync)",
#     )

#     # ------------------------------------------------------------------
#     # CRM reference
#     # ------------------------------------------------------------------
#     crm_agent_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Agent ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Attributes
#     # ------------------------------------------------------------------
#     name: Mapped[str] = mapped_column(
#         String(200),
#         nullable=False,
#         comment="Agent display name",
#     )
#     email: Mapped[str | None] = mapped_column(
#         String(255),
#         nullable=True,
#         default=None,
#         comment="CRM may not always expose agent email",
#     )
#     is_active: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=True,
#         comment="Whether the agent is currently active",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
#         "Tenant", lazy="noload"
#     )
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="agents"
#     )
#     tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
#         "Ticket", back_populates="agent"
#     )
#     user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
#         "UserAgentMapping", back_populates="agent"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Agent id={self.id} name={self.name!r} "
#             f"active={self.is_active} crm_id={self.crm_agent_id!r}>"
#         )

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