# """
# app/main.py

# FastAPI application entry point.

# Startup sequence:
#   1. Configure logging
#   2. Register global exception handlers
#   3. Create all DB tables
#   4. Seed lookup tables if empty
#   5. Run initial full CRM sync
#   6. Start scheduled CRM sync
#   7. Register routers
# """

# from __future__ import annotations

# import logging
# from contextlib import asynccontextmanager

# from fastapi import FastAPI
# from fastapi.middleware.cors import CORSMiddleware
# from sqlalchemy import select
# from sqlalchemy.exc import OperationalError

# from app.core.database import async_session_maker, create_tables
# from app.core.logging import configure_logging
# from app.core.settings import get_settings
# from app.routes import sync, tickets, agents, customers, companies
# from app.services.scheduler import run_all_full_sync, start_scheduler, stop_scheduler
# from app.utils.exceptions import register_exception_handlers
# from app.integrations.webhooks.router import router as webhook_router
# from app.integrations.webhooks.seeder import seed_crm_integrations

# settings = get_settings()
# logger = logging.getLogger(__name__)


# # ---------------------------------------------------------------------------
# # Seed lookup tables
# # ---------------------------------------------------------------------------

# async def seed_lookup_tables() -> None:
#     """
#     Insert default values for lookup tables on first run.
#     Safe to call on every startup — skips if data already exists.
#     """
#     from app.models.source_system import SourceSystem
#     from app.models.ticket_priority import TicketPriority
#     from app.models.ticket_status import TicketStatus

#     async with async_session_maker() as db:
#         try:
#             # source_systems
#             if not (await db.execute(select(SourceSystem))).scalars().first():
#                 db.add_all([
#                     SourceSystem(system_name="zammad"),
#                     SourceSystem(system_name="espocrm"),
#                 ])
#                 logger.info("Seeded source_systems")

#             # ticket_statuses
#             if not (await db.execute(select(TicketStatus))).scalars().first():
#                 db.add_all([
#                     TicketStatus(status_name="open"),
#                     TicketStatus(status_name="pending"),
#                     TicketStatus(status_name="closed"),
#                 ])
#                 logger.info("Seeded ticket_statuses")

#             # ticket_priorities
#             if not (await db.execute(select(TicketPriority))).scalars().first():
#                 db.add_all([
#                     TicketPriority(priority_name="low"),
#                     TicketPriority(priority_name="normal"),
#                     TicketPriority(priority_name="high"),
#                     TicketPriority(priority_name="urgent"),
#                 ])
#                 logger.info("Seeded ticket_priorities")

#             await db.commit()
#             logger.info("Lookup tables ready")

#         except Exception as exc:
#             await db.rollback()
#             logger.error("Failed to seed lookup tables: %s", exc)
#             raise


# # ---------------------------------------------------------------------------
# # Lifespan
# # ---------------------------------------------------------------------------

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     configure_logging()
#     logger.info(
#         "Starting %s v%s [%s]",
#         settings.APP_NAME,
#         settings.APP_VERSION,
#         settings.ENVIRONMENT,
#     )

#     try:
#         # 1. Database Setup
#         await create_tables()
        
#         # 2. Seeding (Lookup Data + CRM Integrations)
#         await seed_lookup_tables()
#         await seed_crm_integrations()
        
#     except OperationalError:
#         logger.critical(
#             "Startup failed — cannot connect to database. "
#             "Is PostgreSQL running? Check DATABASE_URL in .env"
#         )
#         raise

#     # 3. Background Tasks
#     logger.info("Running initial CRM full sync on startup...")
#     await run_all_full_sync()
    
#     start_scheduler()
#     logger.info("CRM sync scheduler started.")

#     yield

#     # 4. Shutdown
#     stop_scheduler()
#     logger.info("Shutting down %s", settings.APP_NAME)


# # ---------------------------------------------------------------------------
# # App instance
# # ---------------------------------------------------------------------------

# app = FastAPI(
#     title=settings.APP_NAME,
#     version=settings.APP_VERSION,
#     description="Unified CRM — aggregates tickets from Zammad and EspoCRM",
#     docs_url="/docs",
#     redoc_url="/redoc",
#     lifespan=lifespan,
# )

# # Global exception handlers — must be registered before any routes
# register_exception_handlers(app)

# # CORS
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=settings.allowed_origins_list,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # Routers
# app.include_router(tickets.router, prefix="/api/v1")
# app.include_router(agents.router, prefix="/api/v1")
# app.include_router(customers.router, prefix="/api/v1")
# app.include_router(companies.router, prefix="/api/v1")
# app.include_router(sync.router, prefix="/api/v1")
# app.include_router(webhook_router)


# # ---------------------------------------------------------------------------
# # Health check
# # ---------------------------------------------------------------------------

# @app.get("/health", tags=["Health"])
# async def health():
#     return {
#         "status": "ok",
#         "app": settings.APP_NAME,
#         "version": settings.APP_VERSION,
#         "environment": settings.ENVIRONMENT,
#     }


# @app.get("/", tags=["Health"])
# async def root():
#     return {
#         "message": f"Welcome to {settings.APP_NAME}",
#         "docs": "/docs",
#     }

"""
app/main.py

FastAPI application entry point.

Startup sequence:
  1. Configure logging
  2. Register global exception handlers
  3. Create all DB tables
  4. Seed lookup tables if empty
  5. Run initial full CRM sync
  6. Start scheduled CRM sync
  7. Register routers
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.database import async_session_maker, create_tables
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.routes import sync, tickets, agents, customers, companies
from app.services.scheduler import run_all_full_sync, start_scheduler, stop_scheduler
from app.utils.exceptions import register_exception_handlers
from app.integrations.webhooks.router import router as webhook_router
settings = get_settings()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed lookup tables
# ---------------------------------------------------------------------------

async def seed_lookup_tables() -> None:
    """
    Insert default values for lookup tables on first run.
    Safe to call on every startup — skips if data already exists.
    """
    from app.models.source_system import SourceSystem
    from app.models.ticket_priority import TicketPriority
    from app.models.ticket_status import TicketStatus

    async with async_session_maker() as db:
        try:
            # source_systems
            if not (await db.execute(select(SourceSystem))).scalars().first():
                db.add_all([
                    SourceSystem(system_name="zammad"),
                    SourceSystem(system_name="espocrm"),
                ])
                logger.info("Seeded source_systems")

            # ticket_status
            if not (await db.execute(select(TicketStatus))).scalars().first():
                db.add_all([
                    TicketStatus(status_name="open"),
                    TicketStatus(status_name="pending"),
                    TicketStatus(status_name="closed"),
                ])
                logger.info("Seeded ticket_status")

            # ticket_priority
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
    except OperationalError:
        logger.critical(
            "Startup failed — cannot connect to database. "
            "Is PostgreSQL running? Check DATABASE_URL in .env"
        )
        raise

    # Initial sync on boot, then start recurring scheduler
    logger.info("Running initial CRM full sync on startup...")
    await run_all_full_sync()
    start_scheduler()
    logger.info("CRM sync scheduler started.")

    yield

    stop_scheduler()
    logger.info("Shutting down %s", settings.APP_NAME)


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Unified CRM — aggregates tickets from Zammad and EspoCRM",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Global exception handlers — must be registered before any routes
register_exception_handlers(app)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(tickets.router,   prefix="/api/v1")
app.include_router(agents.router,    prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(companies.router, prefix="/api/v1")
app.include_router(sync.router,      prefix="/api/v1")
app.include_router(webhook_router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

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
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/docs",
    }