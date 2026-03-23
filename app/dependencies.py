"""
app/dependencies/database_dependency.py

FastAPI dependency that provides an async DB session per request.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker

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