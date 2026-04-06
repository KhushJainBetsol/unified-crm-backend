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
