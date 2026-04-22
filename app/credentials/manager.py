"""
InfisicalCredentialManager  (v2 — unified)
==========================================
This version merges two responsibilities:

  1. AES KEY MANAGEMENT (existing — untouched)
     Fetches ENCRYPTION_KEY_<version> and ACTIVE_KEY_VERSION secrets.
     Used by EncryptionService for the legacy DB-encryption flow.

  2. CRM CREDENTIAL STORAGE (new — adapter pattern)
     Stores/retrieves full CrmCredentialEnvelope objects as Infisical secrets
     under the deterministic name CREDS_<integration_id>.
     The database stores ONLY the integration_id UUID — no ciphertext, no keys.

Both live in the same Infisical project but use different secret name patterns:
  AES keys  : ENCRYPTION_KEY_V1, ENCRYPTION_KEY_V2, ACTIVE_KEY_VERSION
  CRM creds : CREDS_<integration_id>

Migration note
--------------
The existing AES key methods (get_active_key_version, get_encryption_key,
get_active_key_and_version) are completely unchanged.
The new credential methods are additive — nothing existing breaks.
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

# Legacy AES key pattern (existing, unchanged)
_KEY_SECRET_TEMPLATE = "ENCRYPTION_KEY_{version}"
_ACTIVE_VERSION_SECRET = "ACTIVE_KEY_VERSION"


def _key_secret_name(version: str) -> str:
    return _KEY_SECRET_TEMPLATE.format(version=version.upper())


# New CRM credential pattern
def _creds_secret_name(integration_id: str) -> str:
    """
    CREDS_<integration_id>
    e.g. CREDS_550e8400-e29b-41d4-a716-446655440000
    """
    return f"CREDS_{integration_id}"


# ---------------------------------------------------------------------------
# InfisicalCredentialManager
# ---------------------------------------------------------------------------

class InfisicalCredentialManager:
    """
    Synchronous Infisical manager.

    Handles both AES key retrieval (legacy) and direct CRM credential
    storage (new adapter pattern).

    Parameters
    ----------
    settings:
        Fully-populated InfisicalSettings, typically via
        InfisicalSettings.from_env().
    """

    def __init__(self, settings: InfisicalSettings) -> None:
        self._settings = settings
        self._client = self._init_client()

    # ------------------------------------------------------------------
    # SDK initialisation — supports both SDK v1.x and v2.x
    # ------------------------------------------------------------------

    def _init_client(self) -> Any:
        """
        Authenticate with Infisical via Universal Auth (Machine Identity).
        Tries modern SDK imports first, falls back to legacy path.
        """
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
                "InfisicalCredentialManager ready "
                "(host=%s, project=%s, env=%s).",
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
    # SECTION 1 — AES KEY MANAGEMENT (existing, unchanged)
    # ==================================================================

    def get_active_key_version(self) -> str:
        """Fetch the ACTIVE_KEY_VERSION secret from Infisical."""
        raw = self._fetch_secret(
            secret_name="ACTIVE_KEY_VERSION",
            context="ACTIVE_KEY_VERSION",
        )
        return raw.strip().lower()

    def get_encryption_key(self, version: str) -> str:
        """Fetch the raw AES encryption key string for *version*."""
        secret_name = _key_secret_name(version)
        raw = self._fetch_secret(
            secret_name=secret_name,
            context=f"encryption key version={version!r}",
        )
        return raw.strip()

    def get_active_key_and_version(self) -> Tuple[str, str]:
        """
        Fetch active version tag AND corresponding key in one logical call.

        Returns
        -------
        (version, raw_key)
            Ready to pass directly into EncryptionService.
        """
        version = self.get_active_key_version()
        raw_key = self.get_encryption_key(version)
        return version, raw_key

    # ==================================================================
    # SECTION 2 — CRM CREDENTIAL STORAGE (new adapter pattern)
    # ==================================================================

    def save_credentials(
        self,
        integration_id: str,
        envelope: CrmCredentialEnvelope,
    ) -> None:
        """
        SAVE FLOW — serialise envelope to JSON and write to Infisical.

        Secret name: CREDS_<integration_id>
        Secret value: JSON string of the full CrmCredentialEnvelope.

        The database stores ONLY the integration_id — no ciphertext,
        no AES keys, no tokens.

        Uses upsert semantics: tries createSecret, falls back to
        updateSecret on conflict.

        Raises
        ------
        CredentialSaveError
            If the Infisical write fails for any reason.
        """
        secret_name = _creds_secret_name(integration_id)
        secret_value = envelope.model_dump_json()

        logger.info(
            "Saving CRM credentials → integration_id='%s' "
            "(crm_type=%s, secret_name=%s).",
            integration_id,
            envelope.crm_type,
            secret_name,
        )

        try:
            self._upsert_secret(secret_name, secret_value)
        except Exception as exc:
            raise CredentialSaveError(integration_id, str(exc)) from exc

        logger.info(
            "Credentials saved for integration_id='%s'.", integration_id
        )

    def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        """
        READ FLOW — retrieve and deserialise credentials for integration_id.

        1. Fetch secret CREDS_<integration_id> from Infisical.
        2. Parse JSON back into a CrmCredentialEnvelope.
        3. Return envelope — caller extracts .to_credential_dict() and
           .base_url to build the HTTP client in-memory.

        Raises
        ------
        CredentialNotFoundError
            If no secret exists for this integration_id.
        CredentialDecodeError
            If the stored value is malformed JSON or fails Pydantic validation.
        """
        secret_name = _creds_secret_name(integration_id)
        logger.debug(
            "Fetching CRM credentials ← integration_id='%s' (secret=%s).",
            integration_id,
            secret_name,
        )
        raw_value = self._fetch_secret(secret_name, integration_id)
        return self._deserialise_envelope(raw_value, integration_id)

    def delete_credentials(self, integration_id: str) -> None:
        """
        DELETE FLOW — remove the Infisical secret for integration_id.

        Idempotent: if the secret does not exist, logs a warning and returns
        without raising.

        Raises
        ------
        CredentialDeleteError
            On any unexpected Infisical error.
        """
        secret_name = _creds_secret_name(integration_id)
        logger.info(
            "Deleting CRM credentials for integration_id='%s' (secret=%s).",
            integration_id,
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
            logger.info(
                "Deleted Infisical secret '%s' for integration_id='%s'.",
                secret_name,
                integration_id,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning(
                    "Secret '%s' did not exist — nothing to delete.", secret_name
                )
                return
            raise CredentialDeleteError(integration_id, str(exc)) from exc

    def rotate_credentials(
        self,
        integration_id: str,
        new_envelope: CrmCredentialEnvelope,
    ) -> CrmCredentialEnvelope:
        """
        ROTATE FLOW — save new credentials and immediately read back to confirm.

        Returns the freshly-retrieved envelope as write confirmation.
        If the read-back fails, the error propagates unchanged.
        """
        logger.info(
            "Rotating credentials for integration_id='%s'.", integration_id
        )
        self.save_credentials(integration_id, new_envelope)
        confirmed = self.get_credentials(integration_id)
        logger.info(
            "Rotation confirmed for integration_id='%s'.", integration_id
        )
        return confirmed

    def credentials_exist(self, integration_id: str) -> bool:
        """
        Return True if Infisical holds a secret for integration_id.
        Never raises — all exceptions return False.
        """
        try:
            self.get_credentials(integration_id)
            return True
        except (CredentialNotFoundError, CredentialDecodeError):
            return False
        except Exception:
            logger.exception(
                "Unexpected error checking credential existence for '%s'.",
                integration_id,
            )
            return False

    # ==================================================================
    # Private helpers — shared by both sections
    # ==================================================================

    def _fetch_secret(self, secret_name: str, context: str) -> str:
        """
        Retrieve a single secret value string from Infisical by name.

        Parameters
        ----------
        secret_name:
            Exact Infisical secret name.
        context:
            Human-readable label used in error messages (integration_id or
            AES key version).

        Raises
        ------
        CredentialNotFoundError
            When the SDK signals the secret does not exist.
        """
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
            raise  # unexpected SDK / network error — let it propagate

        # Use snake_case first (modern SDK), fallback to camelCase
        value: Optional[str] = getattr(secret, "secret_value", getattr(secret, "secretValue", None))
        
        if not value:
            raise CredentialNotFoundError(context)
            
        return value

    def _upsert_secret(self, secret_name: str, secret_value: str) -> None:
        """
        Write a secret to Infisical, creating or updating as needed.

        Tries createSecret first; on conflict falls back to updateSecret.
        """
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
                logger.debug(
                    "Secret '%s' already exists — updating.", secret_name
                )
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
                raise  # unexpected — let save_credentials wrap it

    def _deserialise_envelope(
        self, raw_value: str, integration_id: str
    ) -> CrmCredentialEnvelope:
        """
        Parse the raw Infisical secret string back into a CrmCredentialEnvelope.

        Raises
        ------
        CredentialDecodeError
            On JSON parse error or Pydantic validation failure.
        """
        try:
            data: Dict[str, Any] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise CredentialDecodeError(
                integration_id,
                f"JSON parse failed: {exc}",
            ) from exc

        try:
            return CrmCredentialEnvelope.model_validate(data)
        except (ValidationError, Exception) as exc:
            raise CredentialDecodeError(
                integration_id,
                f"Envelope validation failed: {exc}",
            ) from exc