"""
app/core/database.py

Async PostgreSQL engine and session factory using SQLAlchemy + asyncpg.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,
)

async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def create_tables() -> None:
    """
    Create all tables from SQLAlchemy metadata.
    Every model must be imported so SQLAlchemy can resolve
    all foreign keys before create_all runs.

    NOTE: Use Alembic migrations in staging/production.
    """
    try:
        from app.core.base import Base  # noqa: F401

        import app.models.source_system  # noqa: F401
        import app.models.ticket_status  # noqa: F401
        import app.models.ticket_priority  # noqa: F401
        import app.models.role  # noqa: F401
        import app.models.permission  # noqa: F401
        import app.models.company  # noqa: F401
        import app.models.customer  # noqa: F401
        import app.models.agent  # noqa: F401
        import app.models.dashboard_user  # noqa: F401
        import app.models.ticket  # noqa: F401
        import app.models.user_source_system  # noqa: F401
        import app.models.user_role  # noqa: F401
        import app.models.role_permission  # noqa: F401
        import app.models.ticket_sync_log  # noqa: F401
        import app.models.user_agent_mapping  # noqa: F401
        import app.models.ticket_comment
        import app.models.crm_integration

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("Database tables created successfully")

    except OperationalError as exc:
        logger.critical(
            "Cannot connect to database. "
            "Check DATABASE_URL in .env and ensure PostgreSQL is running. Error: %s",
            exc,
        )
        raise
