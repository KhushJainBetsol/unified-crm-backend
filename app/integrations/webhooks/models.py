"""
app/integrations/webhooks/models.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class RawWebhookPayload:
    """
    Output of a verified, parsed webhook request.
    Passed from router → service, carrying everything needed for processing.

    UUIDS & IDENTIFIERS
    -------------------
    webhook_uuid        The unique identifier used in the ingest URL.
                        Injected by the router after parse() returns — handlers
                        must not set this. None until router injects it.
    integration_id      CrmIntegration.id — the integration's internal UUID.
                        Stored as plain UUID, not ORM object, to avoid session detachment.

    OTHER FIELDS
    -----------
    source_system_id    CrmIntegration.source_system_id — plain int FK to source_systems.
    source_system       source_system.system_name e.g. "espocrm" | "zammad".
    tenant_id           CrmIntegration.tenant_id — may be None until Keycloak integrated.
    event_type          CRM-native event string (e.g. "Case.create", "ticket.update").
    records             Raw dicts exactly as the CRM sent them, always a list.
    meta                Extra envelope data — no required keys.
    """

    integration_id: UUID
    source_system_id: int
    source_system: str
    tenant_id: UUID | None
    event_type: str
    records: list[dict]
    meta: dict = field(default_factory=dict)
    webhook_uuid: UUID | None = None  # injected by router post-parse; handlers must not set this