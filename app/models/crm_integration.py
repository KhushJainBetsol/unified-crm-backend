"""
app/models/crm_integration.py

tenant_id is stored as a plain UUID column — NO foreign key to tenants yet.
The tenants table doesn't exist in the codebase yet.

FUTURE: Once the tenants table is added, create an Alembic migration to add:
    ALTER TABLE crm_integrations
    ADD CONSTRAINT fk_crm_integrations_tenant_id
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class CrmIntegration(Base):
    __tablename__ = "crm_integrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Plain UUID — no FK to tenants yet (tenants table not built yet)
    # FUTURE: add FK via Alembic migration once tenants table exists
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        default=None,
        comment="Tenant owner — FK to tenants.id added via migration once that table exists",
    )

    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Opaque UUID in the ingest URL
    webhook_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid.uuid4,
        comment="Paste into CRM webhook URL: /webhooks/ingest/{webhook_uuid}",
    )

    # Zammad: single shared token sent in X-Zammad-Token
    # EspoCRM: NULL — per-event secrets live in webhook_secrets JSON
    webhook_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

    # EspoCRM: {"Case.create": "s1", "Case.update": "s2", "Case.delete": "s3"}
    # Zammad: NULL
    webhook_secrets: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # API key used by HTTP clients to call the CRM
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Relationships
    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem",
        lazy="joined",
    )

    def __repr__(self) -> str:
        return (
            f"<CrmIntegration id={self.id} "
            f"tenant={self.tenant_id} "
            f"source_system_id={self.source_system_id} "
            f"active={self.is_active}>"
        )
