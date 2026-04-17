# crm/config/__init__.py
"""
Configuration layer public API.

Import from here rather than from the sub-modules directly so that internal
refactors don't break import sites.
"""

from app.config.loader import ConfigLoader, ConfigLoaderError
from app.config.models import (
    AdapterConfig,
    AdapterRegistryEntry,
    AdapterRegistryManifest,
    AuthConfig,
    EndpointConfig,
    FieldMappingConfig,
    HttpConfig,
    PaginationConfig,
)
from app.config.registry import (
    AdapterNotFoundError,
    AdapterRegistry,
    CapabilityNotSupportedError,
)

__all__ = [
    # Loader
    "ConfigLoader",
    "ConfigLoaderError",
    # Models
    "AdapterConfig",
    "AdapterRegistryEntry",
    "AdapterRegistryManifest",
    "AuthConfig",
    "EndpointConfig",
    "FieldMappingConfig",
    "HttpConfig",
    "PaginationConfig",
    # Registry
    "AdapterNotFoundError",
    "AdapterRegistry",
    "CapabilityNotSupportedError",
]