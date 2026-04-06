# """
# Table  activity_logs
# Audit log — records every significant action performed by a dashboard user.
# The details column holds structured before/after JSON for full traceability.
# """

# from __future__ import annotations

# import uuid
# from datetime import datetime

# from sqlalchemy import DateTime, ForeignKey, String, func
# from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
# from sqlalchemy.orm import Mapped, mapped_column, relationship

# from app.core.base import Base


# class ActivityLog(Base):
#     __tablename__ = "activity_logs"

#     # ------------------------------------------------------------------
#     # Primary key
#     # ------------------------------------------------------------------
#     id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         primary_key=True,
#         default=uuid.uuid4,
#         comment="Log entry ID",
#     )

#     # ------------------------------------------------------------------
#     # Scope
#     # ------------------------------------------------------------------
#     tenant_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("tenants.id", ondelete="CASCADE"),
#         nullable=False,
#         index=True,
#         comment="Audit scoping — which tenant this action belongs to",
#     )
#     user_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         ForeignKey("dashboard_users.id", ondelete="CASCADE"),
#         nullable=False,
#         comment="FK → dashboard_users — who performed the action",
#     )

#     # ------------------------------------------------------------------
#     # Action details
#     # ------------------------------------------------------------------
#     action: Mapped[str] = mapped_column(
#         String(100),
#         nullable=False,
#         comment="Action type e.g. 'ticket.update', 'user.invite'",
#     )
#     details: Mapped[dict | None] = mapped_column(
#         JSONB,
#         nullable=True,
#         default=None,
#         comment="Structured before/after data as JSON",
#     )
#     ip_address: Mapped[str | None] = mapped_column(
#         INET,
#         nullable=True,
#         default=None,
#         comment="Network origin of the request",
#     )

#     # ------------------------------------------------------------------
#     # Timestamp
#     # ------------------------------------------------------------------
#     created_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         nullable=False,
#         server_default=func.now(),
#         comment="When this log entry was created",
#     )

#     # ------------------------------------------------------------------
#     # Relationships
#     # ------------------------------------------------------------------
#     tenant: Mapped["Tenant"] = relationship(  # type: ignore[name-defined]
#         "Tenant", lazy="noload"
#     )
#     user: Mapped["DashboardUser"] = relationship(  # type: ignore[name-defined]
#         "DashboardUser", lazy="noload"
#     )

#     def __repr__(self) -> str:
#         return (
#             f"<ActivityLog id={self.id} action={self.action!r} "
#             f"user={self.user_id} tenant={self.tenant_id}>"
#         )

"""
Table  activity_logs
Audit log for dashboard user actions.
details is NOT NULL per updated schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboard_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="e.g. 'ticket.update', 'user.invite'",
    )
    details: Mapped[dict] = mapped_column(
        JSONB, nullable=False,
        comment="Structured before/after data — NOT NULL",
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]
    user: Mapped["DashboardUser"] = relationship("DashboardUser", lazy="noload")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<ActivityLog id={self.id} action={self.action!r} user={self.user_id}>"
