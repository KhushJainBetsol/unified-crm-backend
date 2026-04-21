"""
app/main.py — Updated for Adapter Pattern Integration

Changes:
  + Fixed Infisical boot by passing Pydantic settings directly (avoiding os.environ lookup)
  + AsyncInfisicalCredentialManager initialized in lifespan and stored on app.state
  + CrmAdapterFactory built from registry + credential manager
  + Graceful shutdown: credential manager thread pool closed on teardown
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.database import async_session_maker, create_tables
from app.core.logging import configure_logging
from app.core.settings import get_settings

# Existing routers
from app.routes import sync, tickets, agents, customers, companies

# NEW routers
from app.routes.auth import router as auth_router
from app.routes.invitations import router as invitations_router
from app.routes.tenants import router as tenants_router
from app.routes.super_admin import router as super_admin_router
from app.services.scheduler import run_all_tenants_full_sync, start_scheduler, stop_scheduler
from app.utils.exceptions import register_exception_handlers
from app.integrations.webhooks.router import router as webhook_router
from app.integrations.webhooks.seeder import seed_crm_integrations

settings = get_settings()
logger = logging.getLogger(__name__)


# ── Unchanged seed helpers ─────────────────────────────────────────────────

async def seed_lookup_tables() -> None:
    """Unchanged — inserts source_systems, ticket_status, ticket_priority."""
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
    """Unchanged — seeds the shared Keycloak realm."""
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


# ── NEW: Adapter infrastructure bootstrap ─────────────────────────────────

async def _bootstrap_adapter_factory(app: FastAPI) -> None:
    """
    Initialise the adapter-layer singletons.
    FIXED: Uses the validated 'settings' object instead of os.environ.
    """
    from pathlib import Path
    from app.config.registry import AdapterRegistry
    from app.credentials.async_manager import AsyncInfisicalCredentialManager
    from app.credentials.models import InfisicalSettings
    from app.factory.adapter_factory import CrmAdapterFactory

    # ── 1. Registry ───────────────────────────────────────────────────────
    config_dir = Path(settings.CRM_CONFIG_DIR)
    registry = AdapterRegistry(config_base_dir=config_dir)
    registry.initialise()
    app.state.adapter_registry = registry
    logger.info("CRM adapter registry ready.")

    # ── 2. Credential manager ─────────────────────────────────────────────
    try:
        # BRIDGE: Map Pydantic settings to the Infisical configuration model
        infisical_settings = InfisicalSettings(
            client_id=settings.INFISICAL_CLIENT_ID,
            client_secret=settings.INFISICAL_CLIENT_SECRET,
            project_id=settings.INFISICAL_PROJECT_ID,
            environment=settings.INFISICAL_ENVIRONMENT,
            host=settings.INFISICAL_HOST,
            secret_path=settings.INFISICAL_SECRET_PATH,
        )
        
        credential_manager = await AsyncInfisicalCredentialManager.create(
            settings=infisical_settings,
            max_workers=4,
        )
        app.state.credential_manager = credential_manager
        logger.info("Infisical credential manager ready.")
    except Exception as exc:
        logger.critical("Infisical boot failed: %s", exc)
        raise

    # ── 3. Factory ────────────────────────────────────────────────────────
    app.state.adapter_factory = CrmAdapterFactory(
        registry=registry,
        credential_manager=credential_manager,
    )
    logger.info("CRM adapter factory ready.")


async def _shutdown_adapter_factory(app: FastAPI) -> None:
    manager = getattr(app.state, "credential_manager", None)
    if manager is not None:
        await manager.close()
        logger.info("Infisical credential manager shut down.")


# ── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("Starting %s v%s [%s]", settings.APP_NAME, settings.APP_VERSION, settings.ENVIRONMENT)

    try:
        await create_tables()
        await seed_lookup_tables()
        await seed_crm_integrations()
        await seed_tenant_realms()
        
        # Now boot the adapter layer with initialized credentials
        await _bootstrap_adapter_factory(app)

    except OperationalError:
        logger.critical("Startup failed — check DATABASE_URL in .env")
        raise

    logger.info("Running initial CRM full sync...")
    await run_all_tenants_full_sync()
    start_scheduler()
    
    yield  # Application runs here

    stop_scheduler()
    await _shutdown_adapter_factory(app)
    logger.info("Shutting down %s", settings.APP_NAME)


# ── App instance ───────────────────────────────────────────────────────────

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

app.include_router(auth_router,           prefix="/api/v1")
app.include_router(invitations_router,    prefix="/api/v1")
app.include_router(super_admin_router,    prefix="/api/v1")
app.include_router(tickets.router,        prefix="/api/v1")
app.include_router(agents.router,         prefix="/api/v1")
app.include_router(customers.router,      prefix="/api/v1")
app.include_router(companies.router,      prefix="/api/v1")
app.include_router(sync.router,           prefix="/api/v1")
app.include_router(tenants_router,        prefix="/api/v1")
app.include_router(webhook_router)

@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "app": settings.APP_NAME}

@app.get("/", tags=["Health"])
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}", "docs": "/docs"}


async def _bootstrap_adapter_factory(app: FastAPI) -> None:
    """
    Boot the adapter layer and attach three objects to app.state:
 
      app.state.adapter_registry       → AdapterRegistry
      app.state.credential_service     → AsyncDbBackedCredentialService
      app.state.adapter_factory        → CrmAdapterFactory
 
    The factory's credential_manager parameter now receives the
    AsyncDbBackedCredentialService which:
      - reads CrmIntegration rows from PostgreSQL
      - fetches the AES key from Infisical
      - decrypts credential_enc in-memory
      - returns a CrmCredentialEnvelope to the factory
 
    No credentials are stored in Infisical.
    Infisical stores only ENCRYPTION_KEY_<version> and ACTIVE_KEY_VERSION.
    """
    from app.config.registry import AdapterRegistry
    from app.credentials.manager import InfisicalCredentialManager
    from app.credentials.models import InfisicalSettings
    from app.credentials.exceptions import InfisicalConfigError
    from app.credentials.db_credential_service import AsyncDbBackedCredentialService
    from app.factory.adapter_factory import CrmAdapterFactory
    from app.core.database import async_session_maker
    from app.core.settings import get_settings
 
    settings = get_settings()
 
    # ── 1. Registry ───────────────────────────────────────────────────────
    config_dir = Path(settings.CRM_CONFIG_DIR)
    registry = AdapterRegistry(config_base_dir=config_dir)
    registry.initialise()
    app.state.adapter_registry = registry
    logger.info(
        "CRM adapter registry ready. Adapters: %s",
        registry.list_adapter_keys(),
    )
 
    # ── 2. Infisical key manager (AES keys only) ──────────────────────────
    try:
        infisical_settings = InfisicalSettings.from_env()
        key_manager = InfisicalCredentialManager(infisical_settings)
    except InfisicalConfigError as exc:
        logger.critical(
            "Infisical configuration error — app cannot start: %s", exc
        )
        raise
 
    # ── 3. Thread pool for sync SDK + AES calls ───────────────────────────
    executor = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="infisical-worker",
    )
    app.state.infisical_executor = executor
 
    # ── 4. Async credential service — DB + Infisical bridge ──────────────
    credential_service = AsyncDbBackedCredentialService(
        key_manager=key_manager,
        async_session_factory=async_session_maker,
        executor=executor,
    )
    app.state.credential_service = credential_service
    logger.info("DB-backed credential service ready.")
 
    # ── 5. Factory ────────────────────────────────────────────────────────
    app.state.adapter_factory = CrmAdapterFactory(
        registry=registry,
        credential_manager=credential_service,
    )
    logger.info("CRM adapter factory ready.")
 
 
async def _shutdown_adapter_factory(app: FastAPI) -> None:
    """Shut down the thread pool on app teardown."""
    executor = getattr(app.state, "infisical_executor", None)
    if executor is not None:
        executor.shutdown(wait=True)
        logger.info("Infisical thread pool shut down.")