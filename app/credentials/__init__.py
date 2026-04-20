"""
Credential management layer public API.

The async manager is the preferred entry point for FastAPI services.
The sync manager is available for scripts, migrations, and CLI tools.
"""

from app.credentials.async_manager import AsyncInfisicalCredentialManager
from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialDeleteError,
    CredentialError,
    CredentialNotFoundError,
    CredentialSaveError,
    InfisicalConfigError,
)
from app.credentials.manager import InfisicalCredentialManager
from app.credentials.models import InfisicalSettings

__all__ = [
    # Managers
    "AsyncInfisicalCredentialManager",
    "InfisicalCredentialManager",
    # Models
    "InfisicalSettings",
    # Exceptions
    "CredentialDecodeError",
    "CredentialDeleteError",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialSaveError",
    "InfisicalConfigError",
]