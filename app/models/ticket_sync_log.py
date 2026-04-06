"""
Table  ticket_sync_logs
Tracks CRM synchronisation operations.
last_sync_at renamed to synced_at per updated schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base

SYNC_STATUS_SUCCESS = "success"
SYNC_STATUS_FAILED  = "failed"
SYNC_STATUS_PARTIAL = "partial"


class TicketSyncLog(Base):
    __tablename__ = "ticket_sync_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="Timestamp of the sync run (renamed from last_sync_at)",
    )
    sync_status: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="'success' | 'failed' | 'partial'",
    )
    records_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    source_system: Mapped["SourceSystem"] = relationship("SourceSystem", back_populates="sync_logs")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return (
            f"<TicketSyncLog id={self.id} source={self.source_system_id} "
            f"status={self.sync_status!r} fetched={self.records_fetched}>"
        )