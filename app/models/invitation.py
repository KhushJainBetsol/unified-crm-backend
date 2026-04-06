"""
Table  invitations
Invitation links sent to admins and agents.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
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
        CheckConstraint(
            "role IN ('admin', 'agent')",
            name="chk_invitations_role",
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
    role: Mapped[str] = mapped_column(
        String(50), nullable=False, default="admin",
        comment="admin | agent",
    )
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
    realm_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
        comment="Keycloak realm this invite belongs to",
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Invitation id={self.id} email={self.email!r} role={self.role!r} status={self.status!r}>"