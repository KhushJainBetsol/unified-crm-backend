"""
app/main.py  — UPDATED for multitenancy

Changes from original:
  + Import and register auth, invitations, super_admin routers
  + Seed tenant_realms table on startup (shared unified-crm realm)
  + Import new tenant models so create_all picks them up

All existing startup logic (sync, scheduler, lookup seeding) is UNTOUCHED.
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
from app.services.scheduler import run_all_full_sync, start_scheduler, stop_scheduler
from app.utils.exceptions import register_exception_handlers
from app.integrations.webhooks.router import router as webhook_router
from app.integrations.webhooks.seeder import seed_crm_integrations

settings = get_settings()
logger = logging.getLogger(__name__)


async def seed_lookup_tables() -> None:
    """Unchanged from original — inserts source_systems, ticket_status, ticket_priority."""
    from app.models.source_system import SourceSystem
    from app.models.ticket_priority import TicketPriority
    from app.models.ticket_status import TicketStatus

    async with async_session_maker() as db:
        try:
            if not (await db.execute(select(SourceSystem))).scalars().first():
                db.add_all(
                    [
                        SourceSystem(system_name="zammad"),
                        SourceSystem(system_name="espocrm"),
                    ]
                )
                logger.info("Seeded source_systems")

            if not (await db.execute(select(TicketStatus))).scalars().first():
                db.add_all(
                    [
                        TicketStatus(status_name="open"),
                        TicketStatus(status_name="pending"),
                        TicketStatus(status_name="closed"),
                    ]
                )
                logger.info("Seeded ticket_status")

            if not (await db.execute(select(TicketPriority))).scalars().first():
                db.add_all(
                    [
                        TicketPriority(priority_name="low"),
                        TicketPriority(priority_name="normal"),
                        TicketPriority(priority_name="high"),
                        TicketPriority(priority_name="urgent"),
                    ]
                )
                logger.info("Seeded ticket_priority")

            await db.commit()
            logger.info("Lookup tables ready")
        except Exception as exc:
            await db.rollback()
            logger.error("Failed to seed lookup tables: %s", exc)
            raise


async def seed_tenant_realms() -> None:
    """
    NEW — Seeds the shared Keycloak realm into tenant_realms on first boot.
    Safe to call on every startup — skips if already seeded.
    """
    async with async_session_maker() as db:
        try:
            result = await db.execute(
                text("SELECT id FROM tenant_realms WHERE realm_name = :realm"),
                {"realm": settings.KEYCLOAK_REALM},
            )
            if not result.fetchone():
                await db.execute(
                    text(
                        """
                        INSERT INTO tenant_realms (id, tenant_id, realm_name, issuer_url, is_active, created_at)
                        VALUES (gen_random_uuid(), NULL, :realm, :issuer, true, now())
                    """
                    ),
                    {
                        "realm": settings.KEYCLOAK_REALM,
                        "issuer": f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
                    },
                )
                await db.commit()
                logger.info(
                    "Seeded tenant_realms with realm: %s", settings.KEYCLOAK_REALM
                )
            else:
                logger.info("tenant_realms already seeded")
        except Exception as exc:
            logger.error("Failed to seed tenant_realms: %s", exc)
            # Don't raise — app can still run, auth will fail gracefully


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "Starting %s v%s [%s]",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.ENVIRONMENT,
    )

    try:
        await create_tables()
        await seed_lookup_tables()
        await seed_crm_integrations()
        await seed_tenant_realms()  # NEW
    except OperationalError:
        logger.critical(
            "Startup failed — cannot connect to database. "
            "Is PostgreSQL running? Check DATABASE_URL in .env"
        )
        raise

    logger.info("Running initial CRM full sync on startup...")
    await run_all_full_sync()
    start_scheduler()
    logger.info("CRM sync scheduler started.")

    yield

    stop_scheduler()
    logger.info("Shutting down %s", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Unified CRM — aggregates tickets from Zammad and EspoCRM",
    docs_url="/docs",
    redoc_url="/redoc",
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

# ── Routers ────────────────────────────────────────────────────────────────
# NEW auth routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(invitations_router, prefix="/api/v1")
app.include_router(super_admin_router, prefix="/api/v1")

# Existing routers (unchanged)
app.include_router(tickets.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(companies.router, prefix="/api/v1")
app.include_router(sync.router, prefix="/api/v1")
app.include_router(tenants_router, prefix="/api/v1")
app.include_router(webhook_router)


@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/", tags=["Health"])
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}", "docs": "/docs"}
