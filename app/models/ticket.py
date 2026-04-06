# """
# app/models/ticket.py

# Table tickets

# CHECK constraints:
#   1. Soft-delete consistency:
#        is_deleted=FALSE → deleted_at must be NULL
#        is_deleted=TRUE  → deleted_at must be NOT NULL

#   2. Deletion-source exclusivity (only enforced when is_deleted=TRUE):
#        is_deleted=FALSE                              → no restriction
#        is_deleted=TRUE AND deleted_by_source=FALSE   → deleted_by_id must be NOT NULL (user deleted)
#        is_deleted=TRUE AND deleted_by_source=TRUE    → deleted_by_id must be NULL (CRM deleted)
# """

# from __future__ import annotations

# import uuid
# from datetime import datetime

# from sqlalchemy import (
#     Boolean,
#     CheckConstraint,
#     DateTime,
#     ForeignKey,
#     String,
#     Text,
#     UniqueConstraint,
#     func,
# )
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Ticket(Base):
#     __tablename__ = "tickets"
#     __table_args__ = (
#         UniqueConstraint(
#             "crm_ticket_id",
#             "source_system_id",
#             name="uq_ticket_crm_source",
#         ),
#         # soft-delete consistency
#         CheckConstraint(
#             "(is_deleted = FALSE AND deleted_at IS NULL) "
#             "OR (is_deleted = TRUE AND deleted_at IS NOT NULL)",
#             name="ck_ticket_soft_delete_consistency",
#         ),
#         # deletion source — only enforced when ticket IS deleted
#         CheckConstraint(
#             "(is_deleted = FALSE) "
#             "OR (is_deleted = TRUE AND deleted_by_source = FALSE AND deleted_by_id IS NOT NULL) "
#             "OR (is_deleted = TRUE AND deleted_by_source = TRUE AND deleted_by_id IS NULL)",
#             name="ck_ticket_deletion_source",
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
#     crm_ticket_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Ticket ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Core ticket fields
#     # ------------------------------------------------------------------
#     title: Mapped[str] = mapped_column(
#         String(255),
#         nullable=False,
#         comment="Ticket subject / title",
#     )
#     description: Mapped[str | None] = mapped_column(
#         Text,
#         nullable=True,
#         default=None,
#         comment="Full ticket description (optional)",
#     )

#     # ------------------------------------------------------------------
#     # Status & priority
#     # ------------------------------------------------------------------
#     status_id: Mapped[int] = mapped_column(
#         ForeignKey("ticket_status.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → ticket_status",
#     )
#     priority_id: Mapped[int | None] = mapped_column(
#         ForeignKey("ticket_priority.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#         comment="FK → ticket_priority (optional)",
#     )

#     # ------------------------------------------------------------------
#     # Related entities
#     # ------------------------------------------------------------------
#     company_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("companies.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     customer_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("customers.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     agent_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("agents.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )

#     # ------------------------------------------------------------------
#     # Timestamps
#     # ------------------------------------------------------------------
#     created_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#     )
#     updated_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#         onupdate=func.now(),
#     )
#     closed_at: Mapped[datetime | None] = mapped_column(
#         DateTime(timezone=True),
#         nullable=True,
#         default=None,
#     )

#     # ------------------------------------------------------------------
#     # Soft-delete fields
#     # ------------------------------------------------------------------
#     is_deleted: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=False,
#     )
#     deleted_at: Mapped[datetime | None] = mapped_column(
#         DateTime(timezone=True),
#         nullable=True,
#         default=None,
#     )
#     deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("dashboard_users.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     deleted_by_source: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=False,
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="tickets"
#     )
#     status: Mapped["TicketStatus"] = relationship(  # type: ignore[name-defined]
#         "TicketStatus", back_populates="tickets"
#     )
#     priority: Mapped["TicketPriority | None"] = relationship(  # type: ignore[name-defined]
#         "TicketPriority", back_populates="tickets"
#     )
#     company: Mapped["Company | None"] = relationship(  # type: ignore[name-defined]
#         "Company", back_populates="tickets"
#     )
#     customer: Mapped["Customer | None"] = relationship(  # type: ignore[name-defined]
#         "Customer", back_populates="tickets"
#     )
#     agent: Mapped["Agent | None"] = relationship(  # type: ignore[name-defined]
#         "Agent", back_populates="tickets"
#     )
#     deleted_by: Mapped["DashboardUser | None"] = relationship(  # type: ignore[name-defined]
#         "DashboardUser",
#         foreign_keys=[deleted_by_id],
#         back_populates="deleted_tickets",
#     )
#      # Relationship to comments (back_populates on TicketComment.ticket)
#     comments: Mapped[list["TicketComment"]] = relationship(
#         "TicketComment",
#         back_populates="ticket",
#         lazy="noload",
#         cascade="all, delete-orphan",
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Ticket id={self.id} crm_id={self.crm_ticket_id!r} "
#             f"title={self.title!r} deleted={self.is_deleted}>"
#         )

# """
# app/models/ticket.py

# Table tickets

# CHECK constraints:
#   1. Soft-delete consistency:
#        is_deleted=FALSE → deleted_at must be NULL
#        is_deleted=TRUE  → deleted_at must be NOT NULL

#   2. Deletion-source exclusivity (only enforced when is_deleted=TRUE):
#        is_deleted=FALSE                              → no restriction
#        is_deleted=TRUE AND deleted_by_source=FALSE   → deleted_by_id must be NOT NULL (user deleted)
#        is_deleted=TRUE AND deleted_by_source=TRUE    → deleted_by_id must be NULL (CRM deleted)
# """

# from __future__ import annotations

# import uuid
# from datetime import datetime

# from sqlalchemy import (
#     Boolean,
#     CheckConstraint,
#     DateTime,
#     ForeignKey,
#     String,
#     Text,
#     UniqueConstraint,
#     func,
# )
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class Ticket(Base):
#     __tablename__ = "tickets"
#     __table_args__ = (
#         UniqueConstraint(
#             "tenant_id",
#             "crm_ticket_id",
#             "source_system_id",
#             name="uq_ticket_tenant_crm_source",
#         ),
#         # soft-delete consistency
#         CheckConstraint(
#             "(is_deleted = FALSE AND deleted_at IS NULL) "
#             "OR (is_deleted = TRUE AND deleted_at IS NOT NULL)",
#             name="ck_ticket_soft_delete_consistency",
#         ),
#         # deletion source — only enforced when ticket IS deleted
#         CheckConstraint(
#             "(is_deleted = FALSE) "
#             "OR (is_deleted = TRUE AND deleted_by_source = FALSE AND deleted_by_id IS NOT NULL) "
#             "OR (is_deleted = TRUE AND deleted_by_source = TRUE AND deleted_by_id IS NULL)",
#             name="ck_ticket_deletion_source",
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
#     # Tenant — resolved from companies.tenant_id at sync time, not from CRM
#     # ------------------------------------------------------------------
#     tenant_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("tenants.id", ondelete="CASCADE"),
#         nullable=True,
#         index=True,
#         comment="FK → tenants — derived from the company record during sync",
#     )

#     # ------------------------------------------------------------------
#     # CRM reference
#     # ------------------------------------------------------------------
#     crm_ticket_id: Mapped[str] = mapped_column(
#         String(50),
#         nullable=False,
#         comment="Ticket ID as it exists in the source CRM",
#     )
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Core ticket fields
#     # ------------------------------------------------------------------
#     title: Mapped[str] = mapped_column(
#         String(255),
#         nullable=False,
#         comment="Ticket subject / title",
#     )
#     description: Mapped[str | None] = mapped_column(
#         Text,
#         nullable=True,
#         default=None,
#         comment="Full ticket description (optional)",
#     )

#     # ------------------------------------------------------------------
#     # Status & priority
#     # ------------------------------------------------------------------
#     status_id: Mapped[int] = mapped_column(
#         ForeignKey("ticket_status.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → ticket_status",
#     )
#     priority_id: Mapped[int | None] = mapped_column(
#         ForeignKey("ticket_priority.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#         comment="FK → ticket_priority (optional)",
#     )

#     # ------------------------------------------------------------------
#     # Related entities
#     # ------------------------------------------------------------------
#     company_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("companies.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     customer_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("customers.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     agent_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("agents.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )

#     # ------------------------------------------------------------------
#     # Timestamps
#     # ------------------------------------------------------------------
#     created_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#     )
#     updated_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#         onupdate=func.now(),
#     )
#     closed_at: Mapped[datetime | None] = mapped_column(
#         DateTime(timezone=True),
#         nullable=True,
#         default=None,
#     )

#     # ------------------------------------------------------------------
#     # Soft-delete fields
#     # ------------------------------------------------------------------
#     is_deleted: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=False,
#     )
#     deleted_at: Mapped[datetime | None] = mapped_column(
#         DateTime(timezone=True),
#         nullable=True,
#         default=None,
#     )
#     deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("dashboard_users.id", ondelete="SET NULL"),
#         nullable=True,
#         default=None,
#     )
#     deleted_by_source: Mapped[bool] = mapped_column(
#         Boolean,
#         nullable=False,
#         default=False,
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
#         "Tenant", lazy="noload"
#     )
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="tickets"
#     )
#     status: Mapped["TicketStatus"] = relationship(  # type: ignore[name-defined]
#         "TicketStatus", back_populates="tickets"
#     )
#     priority: Mapped["TicketPriority | None"] = relationship(  # type: ignore[name-defined]
#         "TicketPriority", back_populates="tickets"
#     )
#     company: Mapped["Company | None"] = relationship(  # type: ignore[name-defined]
#         "Company", back_populates="tickets"
#     )
#     customer: Mapped["Customer | None"] = relationship(  # type: ignore[name-defined]
#         "Customer", back_populates="tickets"
#     )
#     agent: Mapped["Agent | None"] = relationship(  # type: ignore[name-defined]
#         "Agent", back_populates="tickets"
#     )
#     deleted_by: Mapped["DashboardUser | None"] = relationship(  # type: ignore[name-defined]
#         "DashboardUser",
#         foreign_keys=[deleted_by_id],
#         back_populates="deleted_tickets",
#     )
#      # Relationship to comments (back_populates on TicketComment.ticket)
#     comments: Mapped[list["TicketComment"]] = relationship(
#         "TicketComment",
#         back_populates="ticket",
#         lazy="noload",
#         cascade="all, delete-orphan",
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<Ticket id={self.id} crm_id={self.crm_ticket_id!r} "
#             f"title={self.title!r} deleted={self.is_deleted}>"
#         )

"""
app/models/ticket.py

Table tickets

tenant_id is nullable — isolation logic not yet decided.

CHECK constraints:
  1. Soft-delete consistency
  2. Deletion-source exclusivity
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey,
    Index, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "crm_ticket_id", "source_system_id",
            name="uq_ticket_tenant_crm_source",
        ),
        CheckConstraint(
            "(is_deleted = FALSE AND deleted_at IS NULL) "
            "OR (is_deleted = TRUE AND deleted_at IS NOT NULL)",
            name="ck_ticket_soft_delete_consistency",
        ),
        CheckConstraint(
            "(is_deleted = FALSE) "
            "OR (is_deleted = TRUE AND is_deleted_by_crm = FALSE AND deleted_by_id IS NOT NULL) "
            "OR (is_deleted = TRUE AND is_deleted_by_crm = TRUE  AND deleted_by_id IS NULL)",
            name="ck_ticket_deletion_source",
        ),
        Index("idx_tickets_tenant", "tenant_id"),
        Index("idx_tickets_agent", "tenant_id", "agent_id"),
        Index("idx_tickets_company", "tenant_id", "company_id"),
        Index(
            "idx_tickets_not_deleted", "tenant_id",
            postgresql_where="is_deleted = FALSE",
        ),
    )

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )

    # ------------------------------------------------------------------
    # Tenant — nullable until isolation logic is decided
    # ------------------------------------------------------------------
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="FK → tenants — nullable until multi-tenant isolation logic is finalised",
    )

    # ------------------------------------------------------------------
    # CRM reference
    # ------------------------------------------------------------------
    crm_ticket_id: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Ticket ID as it exists in the source CRM",
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False, comment="FK → source_systems",
    )

    # ------------------------------------------------------------------
    # Core fields
    # ------------------------------------------------------------------
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    # ------------------------------------------------------------------
    # Status & priority
    # ------------------------------------------------------------------
    status_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_status.id", ondelete="RESTRICT"),
        nullable=False, comment="FK → ticket_status",
    )
    priority_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_priority.id", ondelete="SET NULL"),
        nullable=True, default=None, comment="FK → ticket_priority (optional)",
    )

    # ------------------------------------------------------------------
    # Related entities
    # ------------------------------------------------------------------
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True, default=None,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True, default=None,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True, default=None,
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None,
    )

    # ------------------------------------------------------------------
    # Soft-delete
    # ------------------------------------------------------------------
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None,
    )
    deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard_users.id", ondelete="SET NULL"),
        nullable=True, default=None,
    )
    is_deleted_by_crm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="TRUE = deleted from CRM side, FALSE = deleted from dashboard",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    tenant: Mapped["Tenant | None"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]
    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="tickets")  # type: ignore[name-defined]
    status: Mapped["TicketStatus"] = relationship("TicketStatus", back_populates="tickets")  # type: ignore[name-defined]
    priority: Mapped["TicketPriority | None"] = relationship("TicketPriority", back_populates="tickets")  # type: ignore[name-defined]
    company: Mapped["Company | None"] = relationship("Company", back_populates="tickets")  # type: ignore[name-defined]
    customer: Mapped["Customer | None"] = relationship("Customer", back_populates="tickets")  # type: ignore[name-defined]
    agent: Mapped["Agent | None"] = relationship("Agent", back_populates="tickets")  # type: ignore[name-defined]
    deleted_by: Mapped["DashboardUser | None"] = relationship(  # type: ignore[name-defined]
        "DashboardUser", foreign_keys=[deleted_by_id], back_populates="deleted_tickets",
    )
    comments: Mapped[list["TicketComment"]] = relationship(  # type: ignore[name-defined]
        "TicketComment", back_populates="ticket", lazy="noload", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Ticket id={self.id} crm_id={self.crm_ticket_id!r} "
            f"title={self.title!r} deleted={self.is_deleted}>"
        )