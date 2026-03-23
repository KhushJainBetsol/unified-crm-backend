"""
app/models/ticket.py

Table tickets

CHECK constraints:
  1. Soft-delete consistency:
       is_deleted=FALSE → deleted_at must be NULL
       is_deleted=TRUE  → deleted_at must be NOT NULL

  2. Deletion-source exclusivity (only enforced when is_deleted=TRUE):
       is_deleted=FALSE                              → no restriction
       is_deleted=TRUE AND deleted_by_source=FALSE   → deleted_by_id must be NOT NULL (user deleted)
       is_deleted=TRUE AND deleted_by_source=TRUE    → deleted_by_id must be NULL (CRM deleted)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint(
            "crm_ticket_id",
            "source_system_id",
            name="uq_ticket_crm_source",
        ),
        # soft-delete consistency
        CheckConstraint(
            "(is_deleted = FALSE AND deleted_at IS NULL) "
            "OR (is_deleted = TRUE AND deleted_at IS NOT NULL)",
            name="ck_ticket_soft_delete_consistency",
        ),
        # deletion source — only enforced when ticket IS deleted
        CheckConstraint(
            "(is_deleted = FALSE) "
            "OR (is_deleted = TRUE AND deleted_by_source = FALSE AND deleted_by_id IS NOT NULL) "
            "OR (is_deleted = TRUE AND deleted_by_source = TRUE AND deleted_by_id IS NULL)",
            name="ck_ticket_deletion_source",
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
    crm_ticket_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Ticket ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Core ticket fields
    # ------------------------------------------------------------------
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Ticket subject / title",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="Full ticket description (optional)",
    )

    # ------------------------------------------------------------------
    # Status & priority
    # ------------------------------------------------------------------
    status_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_status.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK → ticket_status",
    )
    priority_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_priority.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        comment="FK → ticket_priority (optional)",
    )

    # ------------------------------------------------------------------
    # Related entities
    # ------------------------------------------------------------------
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # ------------------------------------------------------------------
    # Soft-delete fields
    # ------------------------------------------------------------------
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dashboard_users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    deleted_by_source: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem", back_populates="tickets"
    )
    status: Mapped["TicketStatus"] = relationship(  # type: ignore[name-defined]
        "TicketStatus", back_populates="tickets"
    )
    priority: Mapped["TicketPriority | None"] = relationship(  # type: ignore[name-defined]
        "TicketPriority", back_populates="tickets"
    )
    company: Mapped["Company | None"] = relationship(  # type: ignore[name-defined]
        "Company", back_populates="tickets"
    )
    customer: Mapped["Customer | None"] = relationship(  # type: ignore[name-defined]
        "Customer", back_populates="tickets"
    )
    agent: Mapped["Agent | None"] = relationship(  # type: ignore[name-defined]
        "Agent", back_populates="tickets"
    )
    deleted_by: Mapped["DashboardUser | None"] = relationship(  # type: ignore[name-defined]
        "DashboardUser",
        foreign_keys=[deleted_by_id],
        back_populates="deleted_tickets",
    )

    def __repr__(self) -> str:
        return (
            f"<Ticket id={self.id} crm_id={self.crm_ticket_id!r} "
            f"title={self.title!r} deleted={self.is_deleted}>"
        )