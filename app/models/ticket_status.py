"""
Table  ticket_status
Prevents inconsistent status values across CRM systems.
Seeded values: 'open', 'pending', 'closed'
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class TicketStatus(Base):
    __tablename__ = "ticket_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        comment="Unique status label, e.g. open / pending / closed",
    )

    tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="status"
    )

    def __repr__(self) -> str:
        return f"<TicketStatus id={self.id} name={self.status_name!r}>"