"""
app/dependencies/database_dependency.py

FastAPI dependency that provides an async DB session per request,
and a shared AsyncInfisicalCredentialManager from app.state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.credentials.async_manager import AsyncInfisicalCredentialManager

logger = logging.getLogger(__name__)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a database session for the duration of a single request.

    - Opens session at request start
    - Commits on success
    - Rolls back on any exception
    - Always closes the session

    Raises:
        503 if the database is unreachable (OperationalError)
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except OperationalError as exc:
            await session.rollback()
            logger.error("Database connection lost during request: %s", exc)
            raise
        except SQLAlchemyError as exc:
            await session.rollback()
            logger.error("Database error during request: %s", exc)
            raise
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def get_key_manager(request: Request) -> AsyncInfisicalCredentialManager:
    """
    Return the shared AsyncInfisicalCredentialManager stored on app.state.

    The manager is initialised once at application startup (see main.py)
    and reused across all requests — it owns the Infisical thread pool.

    Usage in a route:
        key_manager: AsyncInfisicalCredentialManager = Depends(get_key_manager)

    Requires in main.py:
        @app.on_event("startup")
        async def startup():
            app.state.key_manager = await AsyncInfisicalCredentialManager.create()

        @app.on_event("shutdown")
        async def shutdown():
            await app.state.key_manager.close()
    """
    return request.app.state.key_manager