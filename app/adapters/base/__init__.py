# crm/adapters/base/__init__.py
"""
Base adapter layer public API.
"""

from app.adapters.base.adapter import (
    # AdapterError,
    AuthenticationError,
    BaseCrmAdapter,
)
from app.adapters.base.client import (
    BaseCrmClient,
    CrmAuthError,
    CrmClientError,
    CrmNotFoundError,
    CrmRateLimitError,
    CrmServerError,
)
from app.adapters.base.mapper import SchemaMapper

__all__ = [
    # Adapter ABC
    "AdapterError",
    "AuthenticationError",
    "BaseCrmAdapter",
    # HTTP client
    "BaseCrmClient",
    "CrmAuthError",
    "CrmClientError",
    "CrmNotFoundError",
    "CrmRateLimitError",
    "CrmServerError",
    # Mapper
    "SchemaMapper",
]