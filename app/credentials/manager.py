"""
app/credentials/manager.py
==================================
InfisicalCredentialManager  (v3 — versioned tenant keys)
=========================================================
Key naming conventions
----------------------
  AES keys (global/legacy) : ENCRYPTION_KEY_V1, ENCRYPTION_KEY_V2, ACTIVE_KEY_VERSION
  CRM creds                : CREDS_<integration_id>
  Per-tenant keys          : TENANT_KEY_<tenant_id>_<version>   e.g. TENANT_KEY_<uuid>_v1
  Active tenant version    : TENANT_ACTIVE_VERSION_<tenant_id>  e.g. "v1"

Why versioned tenant keys?
--------------------------
TENANT_KEY_<tenant_id>_v1, TENANT_KEY_<tenant_id>_v2, … allows the 90-day
key-rotation scheduler to:
  1. Generate a new key under the NEXT version (v2, v3, …).
  2. Re-encrypt all CrmIntegration rows for that tenant while the old key
     still exists.
  3. Delete the old versioned key from Infisical once migration is confirmed.
  4. Update TENANT_ACTIVE_VERSION_<tenant_id> to the new version.

The crm_integrations.key_version column now stores the actual version tag
(e.g. "v1") instead of the opaque literal "tenant", so the rotation
scheduler can determine which key to fetch for each row unambiguously.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from app.credentials.exceptions import (
    CredentialDecodeError,
    CredentialDeleteError,
    CredentialNotFoundError,
    CredentialSaveError,
    InfisicalConfigError,
)
from app.credentials.models import CrmCredentialEnvelope, InfisicalSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret name conventions — single source of truth
# ---------------------------------------------------------------------------

_KEY_SECRET_TEMPLATE        = "ENCRYPTION_KEY_{version}"
_ACTIVE_VERSION_SECRET      = "ACTIVE_KEY_VERSION"
_TENANT_KEY_TEMPLATE        = "TENANT_KEY_{tenant_id}_{version}"
_TENANT_ACTIVE_VERSION_TMPL = "TENANT_ACTIVE_VERSION_{tenant_id}"


def _key_secret_name(version: str) -> str:
    return _KEY_SECRET_TEMPLATE.format(version=version.upper())


def _tenant_key_secret_name(tenant_id: str, version: str) -> str:
    """
    TENANT_KEY_<tenant_id>_<version>
    e.g. TENANT_KEY_550e8400-e29b-41d4-a716-446655440000_v1
    """
    return _TENANT_KEY_TEMPLATE.format(tenant_id=tenant_id, version=version)


def _tenant_active_version_secret_name(tenant_id: str) -> str:
    """
    TENANT_ACTIVE_VERSION_<tenant_id>
    Stores the current active version string ("v1", "v2", …) for a tenant.
    """
    return _TENANT_ACTIVE_VERSION_TMPL.format(tenant_id=tenant_id)


def _creds_secret_name(integration_id: str) -> str:
    return f"CREDS_{integration_id}"


# ---------------------------------------------------------------------------
# InfisicalCredentialManager
# ---------------------------------------------------------------------------

class InfisicalCredentialManager:
    """
    Synchronous Infisical manager.

    Handles AES key retrieval (legacy global keys) and per-tenant versioned
    key management for the adapter encryption flow.
    """

    def __init__(self, settings: InfisicalSettings) -> None:
        self._settings = settings
        self._client = self._init_client()

    # ------------------------------------------------------------------
    # SDK initialisation
    # ------------------------------------------------------------------

    def _init_client(self) -> Any:
        try:
            from infisical_client import (          # type: ignore[import]
                AuthenticationOptions,
                ClientSettings,
                InfisicalClient,
                UniversalAuthMethod,
            )
        except ImportError:
            try:
                from infisical_client import (      # type: ignore[import]
                    ClientSettings,
                    InfisicalClient,
                )
                from infisical_client.models import (   # type: ignore[import]
                    AuthenticationOptions,
                    UniversalAuthMethod,
                )
            except ImportError as exc:
                raise InfisicalConfigError(
                    f"Infisical SDK import failed: {exc}. "
                    "Run: pip install infisical-python"
                ) from exc

        try:
            client = InfisicalClient(
                settings=ClientSettings(
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
                "InfisicalCredentialManager ready (host=%s, project=%s, env=%s).",
                self._settings.host,
                self._settings.project_id,
                self._settings.environment,
            )
            return client
        except Exception as exc:
            raise InfisicalConfigError(
                f"Failed to initialise Infisical client: {exc}"
            ) from exc

    # ==================================================================
    # SECTION 1 — GLOBAL AES KEY MANAGEMENT (legacy, unchanged)
    # ==================================================================

    def get_active_key_version(self) -> str:
        raw = self._fetch_secret("ACTIVE_KEY_VERSION", context="ACTIVE_KEY_VERSION")
        return raw.strip().lower()

    def get_encryption_key(self, version: str) -> str:
        secret_name = _key_secret_name(version)
        raw = self._fetch_secret(secret_name, context=f"encryption key version={version!r}")
        return raw.strip()

    def get_active_key_and_version(self) -> Tuple[str, str]:
        version = self.get_active_key_version()
        raw_key = self.get_encryption_key(version)
        return version, raw_key

    # ==================================================================
    # SECTION 2 — PER-TENANT VERSIONED KEY MANAGEMENT
    # ==================================================================

    def generate_and_store_tenant_key(self, tenant_id: str) -> Tuple[str, str]:
        """
        Generate a 256-bit random AES key for a tenant and store it under
        TENANT_KEY_<tenant_id>_<version>.

        Determines the next version by incrementing from the current active
        version (or starts at "v1" for a brand-new tenant).

        Also updates TENANT_ACTIVE_VERSION_<tenant_id> to the new version.

        Parameters
        ----------
        tenant_id:
            The tenant's UUID string.

        Returns
        -------
        (version, raw_key)
            The version tag (e.g. "v1") and the raw hex key string.
        """
        import secrets as _secrets

        current_version = self._get_tenant_active_version(tenant_id)
        next_version = _next_version(current_version)

        raw_key = _secrets.token_hex(32)   # 256-bit random key
        secret_name = _tenant_key_secret_name(tenant_id, next_version)
        self._upsert_secret(secret_name, raw_key)

        # Update the active-version pointer
        active_version_secret = _tenant_active_version_secret_name(tenant_id)
        self._upsert_secret(active_version_secret, next_version)

        logger.info(
            "Generated and stored tenant key for tenant_id='%s' "
            "(version=%s, secret_name=%s).",
            tenant_id,
            next_version,
            secret_name,
        )
        return next_version, raw_key

    def get_tenant_active_version(self, tenant_id: str) -> Optional[str]:
        """
        Return the active version tag for this tenant (e.g. "v1"), or None
        if no per-tenant key has been created yet.
        """
        return self._get_tenant_active_version(tenant_id)

    def get_tenant_key(self, tenant_id: str, version: str) -> Optional[str]:
        """
        Fetch the raw AES key for the given tenant + version.

        Returns None if the secret does not exist (caller should fall back
        to the global active key or raise as appropriate).

        Parameters
        ----------
        tenant_id:
            The tenant's UUID string.
        version:
            Key version tag, e.g. "v1", "v2".
        """
        secret_name = _tenant_key_secret_name(tenant_id, version)
        try:
            return self._fetch_secret(secret_name, context=f"tenant_key:{tenant_id}:{version}")
        except CredentialNotFoundError:
            logger.debug(
                "No tenant key found for tenant_id='%s' version='%s'.",
                tenant_id,
                version,
            )
            return None

    def get_active_tenant_key_and_version(self, tenant_id: str) -> Optional[Tuple[str, str]]:
        """
        Convenience: fetch (version, raw_key) for a tenant's currently active key.
        Returns None if no per-tenant key exists.
        """
        version = self._get_tenant_active_version(tenant_id)
        if version is None:
            return None
        raw_key = self.get_tenant_key(tenant_id, version)
        if raw_key is None:
            return None
        return version, raw_key

    def delete_tenant_key(self, tenant_id: str, version: str) -> None:
        """
        Delete a specific versioned tenant key from Infisical.

        Used by the rotation scheduler after all rows have been re-encrypted
        with the new key version.

        Parameters
        ----------
        tenant_id:
            The tenant's UUID string.
        version:
            The version to delete (e.g. "v1" — the OLD version being retired).
        """
        secret_name = _tenant_key_secret_name(tenant_id, version)
        logger.info(
            "Deleting old tenant key for tenant_id='%s' version='%s' (secret=%s).",
            tenant_id,
            version,
            secret_name,
        )
        try:
            from infisical_client import DeleteSecretOptions  # type: ignore[import]

            self._client.deleteSecret(
                options=DeleteSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_name=secret_name,
                )
            )
            logger.info("Deleted Infisical secret '%s'.", secret_name)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning("Secret '%s' did not exist — nothing to delete.", secret_name)
                return
            raise CredentialDeleteError(secret_name, str(exc)) from exc

    # ==================================================================
    # SECTION 3 — CRM CREDENTIAL STORAGE (adapter pattern)
    # ==================================================================

    def save_credentials(self, integration_id: str, envelope: CrmCredentialEnvelope) -> None:
        secret_name = _creds_secret_name(integration_id)
        secret_value = envelope.model_dump_json()
        try:
            self._upsert_secret(secret_name, secret_value)
        except Exception as exc:
            raise CredentialSaveError(integration_id, str(exc)) from exc
        logger.info("Credentials saved for integration_id='%s'.", integration_id)

    def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        secret_name = _creds_secret_name(integration_id)
        raw_value = self._fetch_secret(secret_name, integration_id)
        return self._deserialise_envelope(raw_value, integration_id)

    def delete_credentials(self, integration_id: str) -> None:
        secret_name = _creds_secret_name(integration_id)
        try:
            from infisical_client import DeleteSecretOptions  # type: ignore[import]
            self._client.deleteSecret(
                options=DeleteSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_name=secret_name,
                )
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning("Secret '%s' did not exist — nothing to delete.", secret_name)
                return
            raise CredentialDeleteError(integration_id, str(exc)) from exc

    def rotate_credentials(
        self,
        integration_id: str,
        new_envelope: CrmCredentialEnvelope,
    ) -> CrmCredentialEnvelope:
        self.save_credentials(integration_id, new_envelope)
        confirmed = self.get_credentials(integration_id)
        return confirmed

    def credentials_exist(self, integration_id: str) -> bool:
        try:
            self.get_credentials(integration_id)
            return True
        except (CredentialNotFoundError, CredentialDecodeError):
            return False
        except Exception:
            logger.exception(
                "Unexpected error checking credential existence for '%s'.", integration_id
            )
            return False

    # ==================================================================
    # Private helpers
    # ==================================================================

    def _get_tenant_active_version(self, tenant_id: str) -> Optional[str]:
        """Fetch TENANT_ACTIVE_VERSION_<tenant_id>, return None if missing."""
        secret_name = _tenant_active_version_secret_name(tenant_id)
        try:
            val = self._fetch_secret(secret_name, context=f"tenant_active_version:{tenant_id}")
            return val.strip().lower()
        except CredentialNotFoundError:
            return None

    def _fetch_secret(self, secret_name: str, context: str) -> str:
        try:
            from infisical_client import GetSecretOptions   # type: ignore[import]
        except ImportError:
            from infisical_client.models import GetSecretOptions  # type: ignore[import]

        try:
            secret = self._client.getSecret(
                options=GetSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_name=secret_name,
                )
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                raise CredentialNotFoundError(context) from exc
            raise

        value: Optional[str] = getattr(secret, "secret_value", getattr(secret, "secretValue", None))
        if not value:
            raise CredentialNotFoundError(context)
        return value

    def _upsert_secret(self, secret_name: str, secret_value: str) -> None:
        try:
            from infisical_client import (      # type: ignore[import]
                CreateSecretOptions,
                UpdateSecretOptions,
            )
        except ImportError:
            from infisical_client.models import (   # type: ignore[import]
                CreateSecretOptions,
                UpdateSecretOptions,
            )

        create_opts = CreateSecretOptions(
            project_id=self._settings.project_id,
            environment=self._settings.environment,
            secret_name=secret_name,
            secret_value=secret_value,
        )
        try:
            self._client.createSecret(options=create_opts)
            logger.debug("Created Infisical secret '%s'.", secret_name)
        except Exception as create_exc:
            create_msg = str(create_exc).lower()
            if "already exists" in create_msg or "conflict" in create_msg:
                logger.debug("Secret '%s' already exists — updating.", secret_name)
                self._client.updateSecret(
                    options=UpdateSecretOptions(
                        project_id=self._settings.project_id,
                        environment=self._settings.environment,
                        secret_name=secret_name,
                        secret_value=secret_value,
                    )
                )
                logger.debug("Updated Infisical secret '%s'.", secret_name)
            else:
                raise

    def _deserialise_envelope(self, raw_value: str, integration_id: str) -> CrmCredentialEnvelope:
        try:
            data: Dict[str, Any] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise CredentialDecodeError(integration_id, f"JSON parse failed: {exc}") from exc
        try:
            return CrmCredentialEnvelope.model_validate(data)
        except (ValidationError, Exception) as exc:
            raise CredentialDecodeError(integration_id, f"Envelope validation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def _next_version(current: Optional[str]) -> str:
    """
    Compute the next version tag.

    None / missing → "v1"
    "v1"           → "v2"
    "v9"           → "v10"
    """
    if not current:
        return "v1"
    prefix = "v"
    try:
        num = int(current.lstrip(prefix))
        return f"{prefix}{num + 1}"
    except ValueError:
        # Unexpected format — treat as v0 and return v1
        return "v1"