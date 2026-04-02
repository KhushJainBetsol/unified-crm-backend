"""
app/integrations/webhooks/models.py
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawWebhookPayload:
    """
    Output of a verified, parsed webhook request.
    Passed from router → service, carrying everything needed for processing.

    integration_id      crm_integrations.id  — stored as plain UUID, not the ORM
                        object, to avoid session detachment issues.
    source_system_id    crm_integrations.source_system_id — plain int.
    source_system       source_system.system_name e.g. "espocrm" | "zammad"
    tenant_id           crm_integrations.tenant_id — may be None until Keycloak integrated.
    event_type          CRM-native event string.
    records             Raw dicts exactly as the CRM sent them, always a list.
    meta                Extra envelope data — no required keys.
    """

    integration_id: "uuid.UUID"  # type: ignore[name-defined]
    source_system_id: int
    source_system: str
    tenant_id: "uuid.UUID | None"  # type: ignore[name-defined]
    event_type: str
    records: list[dict]
    meta: dict = field(default_factory=dict)
