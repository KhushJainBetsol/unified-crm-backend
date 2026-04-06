"""
Table  invitations
Invitation links sent to admins and agents.
Removed: is_used, accepted_at, invited_by, role fields.
Added: CHECK constraint on status.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted', 'pending', 'rejected')",
            name="chk_invitations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True,
        comment="Secure onboarding token embedded in the invite link",
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        comment="pending | accepted | rejected",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Invitation id={self.id} email={self.email!r} status={self.status!r}>"
