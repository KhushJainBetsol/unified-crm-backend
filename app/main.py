"""
app/main.py
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

# ── Load .env into os.environ BEFORE anything else ───────────────────────────
# This must be the very first thing that runs so that:
#   - from_env() in InfisicalSettings works correctly
#   - os.getenv() calls anywhere in startup see the correct values
# dotenv does NOT overwrite variables already set in the shell environment.
from dotenv import load_dotenv
load_dotenv(override=False)
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.database import async_session_maker, create_tables
from app.core.logging import configure_logging
from app.core.settings import get_settings

# Routers
from app.routes import sync, tickets, agents, customers, companies
from app.routes.auth import router as auth_router
from app.routes.config import router as config_router
from app.routes.invitations import router as invitations_router
from app.routes.tenants import router as tenants_router
from app.routes.super_admin import router as super_admin_router
from app.routes.credentials import router as credential_router
from app.routes.tenant_source_systems import router as tenant_ss_router
from app.services.scheduler import run_all_tenants_full_sync, start_scheduler, stop_scheduler
from app.utils.exceptions import register_exception_handlers
from app.integrations.webhooks.router import router as webhook_router
from app.integrations.webhooks.seeder import seed_crm_integrations

settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed helpers (unchanged)
# ---------------------------------------------------------------------------

async def seed_lookup_tables() -> None:
    from app.models.source_system import SourceSystem
    from app.models.ticket_priority import TicketPriority
    from app.models.ticket_status import TicketStatus

    async with async_session_maker() as db:
        try:
            if not (await db.execute(select(SourceSystem))).scalars().first():
                db.add_all([
                    SourceSystem(system_name="zammad"),
                    SourceSystem(system_name="espocrm"),
                ])
                logger.info("Seeded source_systems")

            if not (await db.execute(select(TicketStatus))).scalars().first():
                db.add_all([
                    TicketStatus(status_name="open"),
                    TicketStatus(status_name="pending"),
                    TicketStatus(status_name="closed"),
                ])
                logger.info("Seeded ticket_status")

            if not (await db.execute(select(TicketPriority))).scalars().first():
                db.add_all([
                    TicketPriority(priority_name="low"),
                    TicketPriority(priority_name="normal"),
                    TicketPriority(priority_name="high"),
                    TicketPriority(priority_name="urgent"),
                ])
                logger.info("Seeded ticket_priority")

            await db.commit()
            logger.info("Lookup tables ready")
        except Exception as exc:
            await db.rollback()
            logger.error("Failed to seed lookup tables: %s", exc)
            raise


async def seed_tenant_realms() -> None:
    async with async_session_maker() as db:
        try:
            result = await db.execute(
                text("SELECT id FROM tenant_realms WHERE realm_name = :realm"),
                {"realm": settings.KEYCLOAK_REALM},
            )
            if not result.fetchone():
                await db.execute(
                    text("""
                        INSERT INTO tenant_realms (id, tenant_id, realm_name, issuer_url, is_active, created_at)
                        VALUES (gen_random_uuid(), NULL, :realm, :issuer, true, now())
                    """),
                    {
                        "realm": settings.KEYCLOAK_REALM,
                        "issuer": f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
                    },
                )
                await db.commit()
                logger.info("Seeded tenant_realms with realm: %s", settings.KEYCLOAK_REALM)
            else:
                logger.info("tenant_realms already seeded")
        except Exception as exc:
            logger.error("Failed to seed tenant_realms: %s", exc)


# ---------------------------------------------------------------------------
# Adapter infrastructure bootstrap
# ---------------------------------------------------------------------------

async def _bootstrap_adapter_factory(app: FastAPI) -> None:
    """
    Boot the adapter layer and attach to app.state:

      app.state.adapter_registry    → AdapterRegistry
      app.state.key_manager         → AsyncInfisicalCredentialManager
      app.state.credential_service  → AsyncDbBackedCredentialService
      app.state.adapter_factory     → CrmAdapterFactory
      app.state.infisical_executor  → ThreadPoolExecutor (for shutdown)

    Uses InfisicalSettings.from_env() — safe because load_dotenv() runs at
    the top of this module, so os.environ is fully populated before this
    function is ever called.
    """
    from app.config.registry import AdapterRegistry
    from app.credentials.async_manager import AsyncInfisicalCredentialManager
    from app.credentials.models import InfisicalSettings
    from app.credentials.exceptions import InfisicalConfigError
    from app.credentials.db_credential_service import AsyncDbBackedCredentialService
    from app.factory.adapter_factory import CrmAdapterFactory

    # ── 1. Adapter registry ───────────────────────────────────────────────
    config_dir = Path(__file__).parent.parent / settings.CRM_CONFIG_DIR
    registry = AdapterRegistry(config_base_dir=config_dir)
    registry.initialise()
    app.state.adapter_registry = registry
    logger.info("CRM adapter registry ready. Adapters: %s", registry.list_adapter_keys())

    # ── 2. Infisical settings — from_env() is correct here because ────────
    #       load_dotenv() at module top has already populated os.environ.
    #       This is identical to how demo_credential_flow.py works.
    try:
        infisical_settings = InfisicalSettings.from_env()
        logger.info(
            "Infisical settings loaded: host=%s project=%s env=%s",
            infisical_settings.host,
            infisical_settings.project_id,
            infisical_settings.environment,
        )
    except InfisicalConfigError as exc:
        logger.critical("Infisical configuration error — app cannot start: %s", exc)
        raise

    # ── 3. Async key manager ──────────────────────────────────────────────
    #       _initialise() boots the thread pool and constructs the sync
    #       InfisicalCredentialManager off the event loop.
    async_key_manager = AsyncInfisicalCredentialManager(
        settings=infisical_settings,
        max_workers=4,
    )
    await async_key_manager._initialise()
    app.state.key_manager = async_key_manager
    app.state.infisical_executor = async_key_manager._executor
    logger.info("Async Infisical key manager ready.")

    # ── 4. Async credential service (used by adapter factory) ─────────────
    credential_service = AsyncDbBackedCredentialService(
        key_manager=async_key_manager._sync_manager,  # sync manager for the service
        async_session_factory=async_session_maker,
        executor=async_key_manager._executor,         # reuse same thread pool
    )
    app.state.credential_service = credential_service
    logger.info("DB-backed credential service ready.")

    # ── 5. Adapter factory ────────────────────────────────────────────────
    app.state.adapter_factory = CrmAdapterFactory(
        registry=registry,
        credential_manager=credential_service,
    )
    logger.info("CRM adapter factory ready.")


async def _shutdown_adapter_factory(app: FastAPI) -> None:
    key_manager = getattr(app.state, "key_manager", None)
    if key_manager is not None:
        await key_manager.close()
        logger.info("Infisical async key manager shut down.")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "Starting %s v%s [%s]",
        settings.APP_NAME, settings.APP_VERSION, settings.ENVIRONMENT,
    )

    try:
        await create_tables()
        await seed_lookup_tables()
        await seed_crm_integrations()
        await seed_tenant_realms()
        await _bootstrap_adapter_factory(app)
    except OperationalError:
        logger.critical("Startup failed — check DATABASE_URL in .env")
        raise

    logger.info("Running initial CRM full sync...")
    await run_all_tenants_full_sync()
    start_scheduler()

    yield  # ← application runs here

    stop_scheduler()
    await _shutdown_adapter_factory(app)
    logger.info("Shutting down %s", settings.APP_NAME)


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,        prefix="/api/v1")
app.include_router(config_router,      prefix="/api/v1")
app.include_router(invitations_router, prefix="/api/v1")
app.include_router(super_admin_router, prefix="/api/v1")
app.include_router(tickets.router,     prefix="/api/v1")
app.include_router(agents.router,      prefix="/api/v1")
app.include_router(customers.router,   prefix="/api/v1")
app.include_router(companies.router,   prefix="/api/v1")
app.include_router(sync.router,        prefix="/api/v1")
app.include_router(tenants_router,     prefix="/api/v1")
app.include_router(credential_router,  prefix="/api/v1")
app.include_router(webhook_router)
app.include_router(tenant_ss_router, prefix="/api/v1")


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "app": settings.APP_NAME}


@app.get("/", tags=["Health"])
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}", "docs": "/docs"}