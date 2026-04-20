"""
app/credentials/manager.py
===========================
InfisicalCredentialManager
--------------------------
Synchronous manager used to fetch AES encryption keys from Infisical.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from app.credentials.exceptions import (
    CredentialNotFoundError,
    InfisicalConfigError,
)
from app.credentials.models import InfisicalSettings

logger = logging.getLogger(__name__)

# Secret name templates
_KEY_SECRET_TEMPLATE = "ENCRYPTION_KEY_{version}"
_ACTIVE_VERSION_SECRET = "ACTIVE_KEY_VERSION"


def _key_secret_name(version: str) -> str:
    """Return the Infisical secret name for a given key version."""
    return _KEY_SECRET_TEMPLATE.format(version=version.upper())


class InfisicalCredentialManager:
    """
    Synchronous manager that fetches AES encryption keys from Infisical.

    Parameters
    ----------
    settings:
        Fully-populated ``InfisicalSettings``.
    """

    def __init__(self, settings: InfisicalSettings) -> None:
        self._settings = settings
        self._client = self._init_client()

    # ------------------------------------------------------------------
    # SDK initialisation (Refactored for SDK v2.x+ Compatibility)
    # ------------------------------------------------------------------

    def _init_client(self) -> Any:
        """
        Authenticate with Infisical via Universal Auth.
        Handles both legacy and modern SDK internal structures.
        """
        try:
            # Try modern (v2.x) top-level imports first
            from infisical_client import (
                ClientSettings,
                InfisicalClient,
                AuthenticationOptions,
                UniversalAuthMethod,
                GetSecretOptions
            )
        except ImportError:
            try:
                # Fallback to legacy (v1.x) model structure
                from infisical_client import ClientSettings, InfisicalClient, GetSecretOptions
                from infisical_client.models import (
                    AuthenticationOptions,
                    UniversalAuthMethod,
                )
            except ImportError as exc:
                logger.error(f"CRITICAL: Infisical SDK import failed: {exc}")
                raise InfisicalConfigError(
                    f"Infisical SDK error: {exc}. Ensure 'infisical-python' is installed."
                ) from exc

        try:
            client = InfisicalClient(
                ClientSettings(
                    auth=AuthenticationOptions(
                        universal_auth=UniversalAuthMethod(
                            client_id=self._settings.client_id,
                            client_secret=self._settings.client_secret,
                        )
                    ),
                    site_url=self._settings.host,
                )
            )
            
            logger.info(
                "InfisicalCredentialManager ready (host=%s, path=%s).",
                self._settings.host,
                self._settings.secret_path,
            )
            return client
            
        except Exception as exc:
            raise InfisicalConfigError(f"Failed to initialise Infisical client: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_active_key_version(self) -> str:
        """Fetch the ``ACTIVE_KEY_VERSION`` secret from Infisical."""
        raw = self._fetch_secret(
            secret_name=_ACTIVE_VERSION_SECRET,
            context="ACTIVE_KEY_VERSION",
        )
        return raw.strip().lower()

    def get_encryption_key(self, version: str) -> str:
        """Fetch the raw encryption key string for *version*."""
        secret_name = _key_secret_name(version)
        raw = self._fetch_secret(
            secret_name=secret_name,
            context=f"encryption key version={version!r}",
        )
        return raw.strip()

    def get_active_key_and_version(self) -> Tuple[str, str]:
        """Fetch active version tag AND corresponding key in one logical call."""
        version = self.get_active_key_version()
        raw_key = self.get_encryption_key(version)
        return version, raw_key

    # ------------------------------------------------------------------
    # Private SDK helper
    # ------------------------------------------------------------------

    def _fetch_secret(self, secret_name: str, context: str) -> str:
        """Retrieve a single secret value from Infisical by name."""
        # Note: Local import to handle potential path differences in venv
        try:
            from infisical_client import GetSecretOptions
        except ImportError:
            from infisical_client.models import GetSecretOptions

        try:
            secret = self._client.getSecret(
                options=GetSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_path=self._settings.secret_path,
                    secret_name=secret_name,
                )
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                raise CredentialNotFoundError(context) from exc
            raise

        value: Optional[str] = getattr(secret, "secretValue", None)
        if not value:
            raise CredentialNotFoundError(context)

        return value