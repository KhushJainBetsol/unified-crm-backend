"""
Table ticket_priority
Standardised ticket priority values.
Seeded values: 'low', 'normal', 'high', 'urgent'
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class TicketPriority(Base):
    __tablename__ = "ticket_priority"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    priority_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        comment="Unique priority label, e.g. low / normal / high / urgent",
    )

    tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="priority"
    )

    def __repr__(self) -> str:
        return f"<TicketPriority id={self.id} name={self.priority_name!r}>"
