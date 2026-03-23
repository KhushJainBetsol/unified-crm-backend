""""
Table source_systems
Stores which CRM system the data comes from.
Seeded values: 'zammad', 'espocrm'
"""
 
from __future__ import annotations
 
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
 
from app.core.base import Base
 
 
class SourceSystem(Base):
    __tablename__ = "source_systems"
 
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="Unique CRM name, e.g. zammad / espocrm",
    )
 
    # ---- back-references ----
    companies: Mapped[list["Company"]] = relationship(  # type: ignore[name-defined]
        "Company", back_populates="source_system"
    )
    customers: Mapped[list["Customer"]] = relationship(  # type: ignore[name-defined]
        "Customer", back_populates="source_system"
    )
    agents: Mapped[list["Agent"]] = relationship(  # type: ignore[name-defined]
        "Agent", back_populates="source_system"
    )
    tickets: Mapped[list["Ticket"]] = relationship(  # type: ignore[name-defined]
        "Ticket", back_populates="source_system"
    )
    sync_logs: Mapped[list["TicketSyncLog"]] = relationship(  # type: ignore[name-defined]
        "TicketSyncLog", back_populates="source_system"
    )
    user_source_systems: Mapped[list["UserSourceSystem"]] = relationship(  # type: ignore[name-defined]
        "UserSourceSystem", back_populates="source_system"
    )
    user_agent_mappings: Mapped[list["UserAgentMapping"]] = relationship(  # type: ignore[name-defined]
        "UserAgentMapping", back_populates="source_system"
    )
 
    def __repr__(self) -> str:
        return f"<SourceSystem id={self.id} name={self.system_name!r}>"