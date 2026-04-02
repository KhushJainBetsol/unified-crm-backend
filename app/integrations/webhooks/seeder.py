"""
app/integrations/webhooks/seeder.py

Seeds crm_integrations from .env at startup.
Delete this file when frontend admin UI is ready — nothing else changes.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.settings import get_settings
from app.models.crm_integration import CrmIntegration
from app.models.source_system import SourceSystem

logger = logging.getLogger(__name__)


async def seed_crm_integrations() -> None:
    settings = get_settings()

    # tenant_id is optional — NULL until Keycloak is integrated
    raw_tenant_id = getattr(settings, "WEBHOOK_TENANT_ID", "")
    tenant_id: uuid.UUID | None = None
    if raw_tenant_id:
        try:
            tenant_id = uuid.UUID(raw_tenant_id)
        except ValueError:
            logger.warning("WEBHOOK_TENANT_ID is not a valid UUID — seeding with NULL")

    integrations_to_seed = [
        {
            "system_name": "espocrm",
            "webhook_uuid_env": getattr(settings, "ESPO_WEBHOOK_UUID", ""),
            "base_url": getattr(settings, "ESPO_BASE_URL", "") or None,
            "api_key": getattr(settings, "ESPO_API_KEY", "") or None,
            "webhook_secret": None,
            "webhook_secrets": {
                "Case.create": getattr(settings, "ESPO_SECRET_CASE_CREATE", "") or None,
                "Case.update": getattr(settings, "ESPO_SECRET_CASE_UPDATE", "") or None,
                "Case.delete": getattr(settings, "ESPO_SECRET_CASE_DELETE", "") or None,
            },
        },
        {
            "system_name": "zammad",
            "webhook_uuid_env": getattr(settings, "ZAMMAD_WEBHOOK_UUID", ""),
            "base_url": getattr(settings, "ZAMMAD_BASE_URL", "") or None,
            "api_key": getattr(settings, "ZAMMAD_API_TOKEN", "") or None,
            "webhook_secret": getattr(settings, "ZAMMAD_WEBHOOK_SECRET", "") or None,
            "webhook_secrets": None,
        },
    ]

    async with async_session_maker() as session:
        async with session.begin():
            for cfg in integrations_to_seed:
                raw_uuid = cfg["webhook_uuid_env"]
                if not raw_uuid:
                    logger.info("No webhook UUID for %s — skipping", cfg["system_name"])
                    continue

                try:
                    webhook_uuid = uuid.UUID(raw_uuid)
                except ValueError:
                    logger.error(
                        "%s webhook UUID invalid: %r — skipping",
                        cfg["system_name"],
                        raw_uuid,
                    )
                    continue

                # Idempotent — skip if already exists
                existing = await session.execute(
                    select(CrmIntegration).where(
                        CrmIntegration.webhook_uuid == webhook_uuid
                    )
                )
                if existing.scalars().first():
                    logger.debug("%s already seeded — skipping", cfg["system_name"])
                    continue

                ss_result = await session.execute(
                    select(SourceSystem).where(
                        SourceSystem.system_name == cfg["system_name"]
                    )
                )
                source_system = ss_result.scalars().first()
                if not source_system:
                    logger.error(
                        "source_systems row '%s' not found — seed it first",
                        cfg["system_name"],
                    )
                    continue

                session.add(
                    CrmIntegration(
                        tenant_id=tenant_id,
                        source_system_id=source_system.id,
                        webhook_uuid=webhook_uuid,
                        base_url=cfg["base_url"],
                        api_key=cfg["api_key"],
                        webhook_secret=cfg["webhook_secret"],
                        webhook_secrets=cfg["webhook_secrets"],
                        is_active=True,
                    )
                )
                logger.info(
                    "Seeded %s integration (webhook_uuid=%s tenant=%s)",
                    cfg["system_name"],
                    webhook_uuid,
                    tenant_id or "NULL",
                )
