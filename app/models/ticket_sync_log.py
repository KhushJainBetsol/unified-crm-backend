# """
# Table  ticket_sync_logs
# Tracks CRM synchronisation operations so that:
#   - Sync failures can be investigated
#   - Partial syncs are auditable
#   - The last successful sync time is queryable per source system
# """

# from __future__ import annotations

# import uuid
# from datetime import datetime

# from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
# from sqlalchemy.dialects.postgresql import UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base

# # Allowed values for sync_status — validated at the service layer.
# # A DB CHECK constraint is intentionally omitted so that new status
# # values can be added without a migration.
# SYNC_STATUS_SUCCESS = "success"
# SYNC_STATUS_FAILED = "failed"
# SYNC_STATUS_PARTIAL = "partial"


# class TicketSyncLog(Base):
#     __tablename__ = "ticket_sync_logs"

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
#     source_system_id: Mapped[int] = mapped_column(
#         ForeignKey("source_systems.id", ondelete="RESTRICT"),
#         nullable=False,
#         comment="FK → source_systems",
#     )

#     # ------------------------------------------------------------------
#     # Sync metadata
#     # ------------------------------------------------------------------
#     last_sync_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         comment="Timestamp of the sync run that produced this log entry",
#     )
#     sync_status: Mapped[str] = mapped_column(
#         String(20),
#         nullable=False,
#         comment="Outcome: 'success' | 'failed' | 'partial'",
#     )
#     records_fetched: Mapped[int] = mapped_column(
#         Integer,
#         nullable=False,
#         default=0,
#         comment="Number of tickets fetched/synced during this run",
#     )
#     error_message: Mapped[str | None] = mapped_column(
#         Text,
#         nullable=True,
#         default=None,
#         comment="Error details when sync_status is 'failed' or 'partial'",
#     )

#     # ------------------------------------------------------------------
#     # Audit timestamp
#     # ------------------------------------------------------------------
#     created_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#         comment="When this log record was inserted",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
#         "SourceSystem", back_populates="sync_logs"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<TicketSyncLog id={self.id} source={self.source_system_id} "
#             f"status={self.sync_status!r} fetched={self.records_fetched}>"
#         )

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