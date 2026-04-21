"""
app/models/crm_integration.py

Stores CRM integration config with AES-256-GCM encrypted credentials.

STORAGE DESIGN (v3 — normalised columns)
------------------------------------------
Non-secret fields are top-level columns (queryable, indexable):
    auth_type, key_version, base_url

Secrets live in TWO separate encrypted columns:
    credential_enc      — outbound auth secrets (shape varies by auth_type)
    webhook_secrets_enc — inbound webhook verification secrets

Both _enc columns store AES-256-GCM encrypted JSON.
The decrypted value is a CRM-specific dict, e.g.:

  auth_type=api_key / bearer_token / access_token:
      credential_enc  -> {"token": "abc123"}

  auth_type=basic_auth:
      credential_enc  -> {"username": "u", "password": "p"}

  auth_type=oauth2:
      credential_enc  -> {
          "access_token": "...",
          "refresh_token": "...",
          "expires_at": 1700000000
      }

  auth_type=hmac:
      credential_enc        -> {"api_token": "..."}          # outbound
      webhook_secrets_enc   -> {                             # inbound
          "webhook_secret": "s1",
          "per_event_secrets": {"Case.create": "s2"}
      }

WHY SEPARATE _ENC COLUMNS
---------------------------
  + Principle of least privilege: services that only verify inbound webhooks
    never need to decrypt outbound credentials, and vice-versa.
  + Simpler rotation: each column can be re-keyed independently.
  + Clearer semantics vs. one opaque blob.

WHY DEDICATED PLAIN COLUMNS (auth_type, key_version, base_url)
----------------------------------------------------------------
  + Directly filterable/indexable in PostgreSQL without decryption.
  + No need to deserialise a JSONB envelope just to read the discriminator.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class CrmIntegration(Base):
    __tablename__ = "crm_integrations"

    # ── Identity ───────────────────────────────────────────────────────────

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="FK -> tenants.id (ON DELETE CASCADE). Enforce via Alembic.",
    )

    source_system_id: Mapped[int] = mapped_column(
        ForeignKey("source_systems.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── Webhook routing ────────────────────────────────────────────────────

    webhook_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid.uuid4,
        comment="Opaque UUID in ingest URL: /webhooks/ingest/{webhook_uuid}",
    )

    # ── Auth discriminator & key tracking (plain — queryable) ──────────────

    auth_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment=(
            "Discriminator for credential shape. "
            "One of: api_key | hmac | bearer_token | access_token | basic_auth | oauth2"
        ),
    )

    key_version: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="v1",
        comment="Tracks which AES key version encrypted the _enc columns.",
    )

    # ── Non-secret base address (plain — queryable) ────────────────────────

    base_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default=None,
        comment="Non-secret CRM base address. Not encrypted.",
    )

    # ── Outbound auth secrets ──────────────────────────────────────────────
    # Stores AES-256-GCM encrypted JSON.
    # Decrypted shape depends on auth_type (see module docstring).

    credential_enc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment=(
            "AES-256-GCM encrypted JSON blob holding outbound auth secrets. "
            "Decrypted shape varies by auth_type (see module docstring)."
        ),
    )

    # ── Inbound webhook verification secrets ──────────────────────────────
    # Stores AES-256-GCM encrypted JSON.
    # Decrypted shape for hmac: {"webhook_secret": "...", "per_event_secrets": {}}

    webhook_secrets_enc: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment=(
            "AES-256-GCM encrypted JSON blob holding inbound webhook secrets. "
            "Decrypted shape varies by CRM (see module docstring)."
        ),
    )

    # ── Token expiry (plaintext — not a secret) ────────────────────────────

    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="NULL = no expiry. Set for OAuth2 / expiring tokens.",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Soft-disable without deleting.",
    )

    # ── Audit ──────────────────────────────────────────────────────────────

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────

    source_system: Mapped["SourceSystem"] = relationship(  # type: ignore[name-defined]
        "SourceSystem",
        lazy="joined",
    )

    # ── Convenience helpers ────────────────────────────────────────────────

    def has_credentials(self) -> bool:
        """True when outbound auth secrets are present."""
        return bool(self.credential_enc)

    def has_webhook_secrets(self) -> bool:
        """True when inbound webhook verification secrets are present."""
        return bool(self.webhook_secrets_enc)

    def __repr__(self) -> str:
        return (
            f"<CrmIntegration id={self.id} "
            f"tenant={self.tenant_id} "
            f"auth_type={self.auth_type} "
            f"key_version={self.key_version} "
            f"active={self.is_active}>"
        )