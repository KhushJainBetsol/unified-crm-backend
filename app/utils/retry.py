"""
app/utils/retry.py

Retry logic for handling transient database errors (race conditions, deadlocks).

Used by the sync/webhook layer to handle cases where concurrent operations
attempt to create the same record, triggering unique constraint violations.

Strategy:
- Exponential backoff: 100ms, 200ms, 400ms, 800ms, 1600ms
- Max 5 retries (3.1 seconds total)
- Only retry on specific errors (IntegrityError, OperationalError)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, TypeVar, Any

from sqlalchemy.exc import IntegrityError, OperationalError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Configuration
MAX_RETRIES = 5
BASE_DELAY_MS = 100


async def retry_on_conflict(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Execute an async function with exponential backoff retry on database conflicts.

    Retries on:
    - IntegrityError (unique constraint violations, foreign key violations)
    - OperationalError (deadlocks, connection issues)

    Usage
    -----
    result = await retry_on_conflict(repo.upsert, crm_id, source_system_id, data)

    Returns:
        Result of the function call on success.

    Raises:
        Last exception if all retries exhausted.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await func(*args, **kwargs)
        except (IntegrityError, OperationalError) as exc:
            if attempt < MAX_RETRIES - 1:
                delay_ms = BASE_DELAY_MS * (2 ** attempt)
                logger.warning(
                    "Database conflict (attempt %d/%d) | retrying in %dms | reason=%s",
                    attempt + 1,
                    MAX_RETRIES,
                    delay_ms,
                    str(exc)[:100],
                )
                await asyncio.sleep(delay_ms / 1000.0)
            else:
                logger.error(
                    "Max retries exhausted (%d) | final error=%s",
                    MAX_RETRIES,
                    str(exc)[:100],
                )
                raise
