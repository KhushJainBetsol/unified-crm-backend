"""
app/utils/response.py

Standardised API response helpers.
Every route uses these so the response envelope is always consistent.

Success:  { "success": true,  "message": "...", "data": {...} }
Error:    { "success": false, "message": "...", "data": null  }
"""

from __future__ import annotations

from typing import Any


def success(message: str, data: Any = None) -> dict:
    """Standard success response."""
    return {"success": True, "message": message, "data": data}


def paginated(
    items: list,
    total: int,
    page: int,
    page_size: int,
    message: str = "Fetched successfully",
) -> dict:
    """Paginated list response with metadata."""
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return success(
        message=message,
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        },
    )