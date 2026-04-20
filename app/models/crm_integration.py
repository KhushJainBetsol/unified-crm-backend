"""
app/models/crm_integration.py

Stores CRM integration config with AES-256 encrypted sensitive fields.

Encrypted columns (_enc suffix)
---------------------------------
All three _enc columns hold a JSON string produced by EncryptionService:
    {
        "iv":        "<base64>",
        "data":      "<base64>",
        "algorithm": "AES-256-CBC",
        "key_version": "v1"
    }
The key used to decrypt is fetched from Infisical at runtime using key_version.

Non-encrypted columns
---------------------
base_url    — CRM host URL; not a secret.
auth_type   — discriminator so the adapter knows how to build the auth header.
key_version — which Infisical key version was used; safe to store in plain text.

FUTURE: tenant_id FK constraint is enforced here. Add Alembic migration if
the tenants table is created after this one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class CrmIntegration(Base):
    __tablename__ = "crm_integrations"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="FK → tenants.id (ON DELETE CASCADE). Add constraint via Alembic migration.",
    )

    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Webhook routing
    # ------------------------------------------------------------------

    webhook_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid.uuid4,
        comment="Opaque UUID embedded in the ingest URL: /webhooks/ingest/{webhook_uuid}",
    )

    # ------------------------------------------------------------------
    # Encrypted sensitive fields
    # ------------------------------------------------------------------
    # Each column stores a JSON string:
    #   {"iv": "<b64>", "data": "<b64>", "algorithm": "AES-256-CBC", "key_version": "v1"}
    # Decrypt by: fetch ENCRYPTION_KEY_<key_version> from Infisical → AES-256-CBC decrypt.

    webhook_secret_enc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment=(
            "AES-256-CBC encrypted webhook secret. "
            "Zammad: single shared token in X-Zammad-Token. "
            "EspoCRM: NULL (per-event secrets live in webhook_secrets_enc)."
        ),
    )

    webhook_secrets_enc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment=(
            "AES-256-CBC encrypted JSON mapping event → secret. "
            'EspoCRM: {"Case.create": "s1", "Case.update": "s2", ...}. '
            "Zammad: NULL."
        ),
    )

    credential_enc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment=(
            "AES-256-CBC encrypted credential payload. "
            "Replaces api_key / token / username+password etc. "
            "Shape depends on auth_type (see auth_type column)."
        ),
    )

    # ------------------------------------------------------------------
    # Credential metadata (plaintext — not secrets)
    # ------------------------------------------------------------------

    auth_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment=(
            "Discriminator for credential kind. "
            "Values: api_key | hmac | bearer_token | access_token | basic_auth | oauth2. "
            "Drives how the auth header is constructed at request time."
        ),
    )

    key_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="v1",
        comment=(
            "Tracks which Infisical key version encrypted the _enc columns. "
            "Used during rotation to select the correct decryption key. "
            "Example values: 'v1', 'v2'."
        ),
    )

    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment=(
            "NULL = no expiry (EspoCRM API key; Zammad token without expiry). "
            "Set for: Zammad tokens with expiry, OAuth2 access tokens. "
            "Background job checks this to proactively re-provision before expiry."
        ),
    )

    base_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="CRM instance URL — not a secret, stored in plain text.",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Soft-disable an integration without deleting it.",
    )

    # ------------------------------------------------------------------
    # Audit timestamps
    # ------------------------------------------------------------------

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Row creation time (set by DB).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Last update time. Keep in sync with a DB trigger or use onupdate.",
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem",
        lazy="joined",
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def has_credentials(self) -> bool:
        """Return True if any encrypted credential column is populated."""
        return any([
            self.credential_enc,
            self.webhook_secret_enc,
            self.webhook_secrets_enc,
        ])

    def __repr__(self) -> str:
        return (
            f"<CrmIntegration id={self.id} "
            f"tenant={self.tenant_id} "
            f"source_system_id={self.source_system_id} "
            f"auth_type={self.auth_type} "
            f"key_version={self.key_version} "
            f"active={self.is_active}>"
        )