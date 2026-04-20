"""
app/integrations/webhooks/seeder.py
Seeds crm_integrations from .env at startup.
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

    # 1. Mandatory Tenant Check
    # crm_integrations.tenant_id is NOT NULL. If missing, we must abort.
    raw_tenant_id = getattr(settings, "WEBHOOK_TENANT_ID", "")
    tenant_id: uuid.UUID | None = None
    
    if raw_tenant_id:
        try:
            tenant_id = uuid.UUID(raw_tenant_id)
        except ValueError:
            logger.error("WEBHOOK_TENANT_ID is invalid. Aborting CRM seeding.")
            return
    
    if not tenant_id:
        logger.error("WEBHOOK_TENANT_ID is missing. Mandatory for multitenancy. Aborting seeding.")
        return

    # 2. Integration Config
    # Removed api_key/secrets. Using auth_type discriminator.
    integrations_to_seed = [
        {
            "system_name": "espocrm",
            "webhook_uuid_env": getattr(settings, "ESPO_WEBHOOK_UUID", ""),
            "base_url": getattr(settings, "ESPO_BASE_URL", "") or None,
            "auth_type": "api_key",
        },
        {
            "system_name": "zammad",
            "webhook_uuid_env": getattr(settings, "ZAMMAD_WEBHOOK_UUID", ""),
            "base_url": getattr(settings, "ZAMMAD_BASE_URL", "") or None,
            "auth_type": "bearer_token",
        },
    ]

    async with async_session_maker() as session:
        async with session.begin():
            for cfg in integrations_to_seed:
                raw_uuid = cfg["webhook_uuid_env"]
                if not raw_uuid:
                    logger.info("No UUID for %s — skipping", cfg["system_name"])
                    continue

                try:
                    webhook_uuid = uuid.UUID(raw_uuid)
                except ValueError:
                    continue

                # 3. Idempotency Check
                existing = await session.execute(
                    select(CrmIntegration).where(CrmIntegration.webhook_uuid == webhook_uuid)
                )
                if existing.scalars().first():
                    logger.debug("%s already exists — skipping", cfg["system_name"])
                    continue

                # 4. Lookup Source System
                ss_result = await session.execute(
                    select(SourceSystem).where(SourceSystem.system_name == cfg["system_name"])
                )
                source_system = ss_result.scalars().first()
                if not source_system:
                    logger.error("Source system '%s' not found. Seed source_systems first.", cfg["system_name"])
                    continue

                # 5. Insert with Encrypted-Schema defaults
                session.add(
                    CrmIntegration(
                        tenant_id=tenant_id,
                        source_system_id=source_system.id,
                        webhook_uuid=webhook_uuid,
                        base_url=cfg["base_url"],
                        auth_type=cfg["auth_type"],
                        key_version="v1",
                        is_active=True,
                        # Sensitive _enc columns default to None.
                        # Populated later via API/EncryptionService.
                    )
                )
                logger.info("Seeded %s integration (tenant=%s)", cfg["system_name"], tenant_id)