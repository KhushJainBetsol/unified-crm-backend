"""
app/models/ticket_comment.py

Stores comments / notes / articles fetched from CRM systems.

CRM mapping:
  Zammad  → ticket_articles  (GET /api/v1/ticket_articles/by_ticket/{crm_ticket_id})
  EspoCRM → stream Posts     (GET /api/v1/Case/{crm_ticket_id}/stream?where[type]=Post)

One row = one comment from either CRM.
The ticket FK links to OUR internal tickets table (UUID).
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, Uuid
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.base import Base


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[_uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )

    # ------------------------------------------------------------------
    # Foreign keys
    # ------------------------------------------------------------------
    ticket_id: Mapped[_uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_system_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # CRM identity
    # ------------------------------------------------------------------
    # The comment's own ID inside the originating CRM
    crm_comment_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "note", "email", "phone", "web", "chat", "Post" …
    comment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Name or email of whoever wrote the comment (may be agent or customer)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # True  = written by an agent / staff member
    # False = written by a customer / requester
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ------------------------------------------------------------------
    # Timestamps (from CRM — preserves original time)
    # ------------------------------------------------------------------
    crm_created_at: Mapped[_uuid.UUID | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    crm_updated_at: Mapped[_uuid.UUID | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------------
    # Our own audit timestamps
    # ------------------------------------------------------------------
    created_at: Mapped[_uuid.UUID] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[_uuid.UUID] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    ticket: Mapped["Ticket"] = relationship(  # noqa: F821
        "Ticket",
        back_populates="comments",
        lazy="noload",
    )
    source_system: Mapped["SourceSystem"] = relationship(  # noqa: F821
        "SourceSystem",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<TicketComment id={self.id} "
            f"ticket_id={self.ticket_id} "
            f"crm_comment_id={self.crm_comment_id!r}>"
        )
