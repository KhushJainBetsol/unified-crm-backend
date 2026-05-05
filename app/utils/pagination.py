"""
app/utils/pagination.py

Centralized pagination parameter validation and bounds enforcement.

All list endpoints should use these utilities to ensure:
- Offset >= 0
- Limit > 0 and <= MAX_LIMIT
- No DoS attacks via huge limit values
"""

from __future__ import annotations

import logging

from fastapi import Query

logger = logging.getLogger(__name__)

# Configuration
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 1


def validate_pagination(
    offset: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=MIN_PAGE_SIZE,
        le=MAX_PAGE_SIZE,
        description=f"Max records to return (max {MAX_PAGE_SIZE})",
    ),
) -> tuple[int, int]:
    """
    Validate and return pagination parameters.

    Ensures:
    - offset >= 0
    - limit is between MIN_PAGE_SIZE and MAX_PAGE_SIZE
    - Raises validation error if constraints violated

    Usage in routes:
    ----
    @router.get("/items")
    async def list_items(
        pagination: tuple[int, int] = Depends(validate_pagination),
        db: AsyncSession = Depends(get_db),
    ):
        offset, limit = pagination
        ...
    """
    logger.debug(
        "Pagination parameters validated | offset=%d | limit=%d",
        offset,
        limit,
    )
    return offset, limit
