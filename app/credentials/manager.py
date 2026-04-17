# crm/credentials/manager.py
"""
InfisicalCredentialManager
===========================
Implements the exact credential lifecycle described in the architecture spec:

  SAVE FLOW
  ---------
  1. Receive a ``CrmCredentialEnvelope`` (plaintext credentials + base_url).
  2. Serialise it to a JSON string.
  3. Call Infisical SDK ``create_secret`` or ``update_secret`` using the
     deterministic secret name ``CREDS_<integration_id>``.
  4. Return — the database only ever stores the ``integration_id``.

  READ FLOW
  ---------
  1. Receive an ``integration_id`` (the only thing stored in the DB).
  2. Call Infisical SDK ``get_secret`` with name ``CREDS_<integration_id>``.
  3. Parse the retrieved JSON string back into a ``CrmCredentialEnvelope``.
  4. Return the envelope — the factory extracts ``.to_credential_dict()``
     and ``.base_url`` to build the adapter in-memory.

  DELETE FLOW
  -----------
  Delete the Infisical secret entirely.  Used when an integration is removed.

  ROTATE FLOW
  -----------
  Convenience wrapper: save new credentials over the existing secret, then
  return the freshly-fetched envelope to confirm the write succeeded.

Thread / concurrency model
--------------------------
The Infisical Python SDK is synchronous.  This manager is therefore also
synchronous.  The async wrapper ``AsyncInfisicalCredentialManager`` in
``async_manager.py`` offloads calls to a thread pool so the FastAPI event
loop is never blocked.

Secret naming convention
------------------------
Secret name : ``CREDS_<integration_id>``
Secret path : ``settings.secret_path``  (default: ``/crm``)
Environment : ``settings.environment``  (default: ``prod``)

All three together uniquely identify one tenant's credentials within the
Infisical project.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

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
# Secret name helper — single source of truth for the naming convention
# ---------------------------------------------------------------------------

def _secret_name(integration_id: str) -> str:
    """
    Deterministically generate the Infisical secret name for an integration.

    Example: integration_id="a1b2c3" → "CREDS_a1b2c3"
    """
    return f"CREDS_{integration_id}"


# ---------------------------------------------------------------------------
# InfisicalCredentialManager
# ---------------------------------------------------------------------------

class InfisicalCredentialManager:
    """
    Synchronous manager for CRM credentials stored in Infisical.

    Parameters
    ----------
    settings:
        Fully-populated ``InfisicalSettings``.  Typically constructed via
        ``InfisicalSettings.from_env()`` at application startup.

    Usage
    -----
    settings = InfisicalSettings.from_env()
    manager  = InfisicalCredentialManager(settings)

    # Save (called once when an integration is provisioned)
    envelope = CrmCredentialEnvelope(
        crm_type="zammad",
        base_url="https://support.acme.com",
        credentials={"strategy": "api_token", "token": "secret123"},
    )
    manager.save_credentials("int-uuid-001", envelope)
    # → DB stores only "int-uuid-001"

    # Read (called at request time by the factory)
    envelope = manager.get_credentials("int-uuid-001")
    client   = factory.create("zammad", envelope)
    """

    def __init__(self, settings: InfisicalSettings) -> None:
        self._settings = settings
        self._client = self._init_client()

    # ------------------------------------------------------------------
    # SDK initialisation
    # ------------------------------------------------------------------

    def _init_client(self) -> Any:
        """
        Initialise and authenticate the Infisical SDK client using
        Universal Auth (Machine Identity — client_id + client_secret).

        Raises
        ------
        InfisicalConfigError
            If the SDK cannot authenticate (wrong credentials, unreachable host).
        """
        try:
            from infisical_client import (           # type: ignore[import]
                ClientSettings,
                InfisicalClient,
            )
            from infisical_client.models import (    # type: ignore[import]
                AuthenticationOptions,
                UniversalAuthMethod,
            )
        except ImportError as exc:
            raise InfisicalConfigError(
                "The 'infisical-python' package is not installed. "
                "Add it to your dependencies: pip install infisical-python"
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
        except Exception as exc:
            raise InfisicalConfigError(
                f"Failed to initialise Infisical client: {exc}"
            ) from exc

        logger.info(
            "InfisicalCredentialManager initialised "
            "(host=%s, project=%s, env=%s, path=%s).",
            self._settings.host,
            self._settings.project_id,
            self._settings.environment,
            self._settings.secret_path,
        )
        return client

    # ------------------------------------------------------------------
    # SAVE FLOW
    # ------------------------------------------------------------------

    def save_credentials(
        self,
        integration_id: str,
        envelope: CrmCredentialEnvelope,
    ) -> None:
        """
        Serialise *envelope* to JSON and write it to Infisical as a secret
        named ``CREDS_<integration_id>``.

        If a secret with that name already exists it is updated (upsert
        semantics via try-create / except-update).

        Parameters
        ----------
        integration_id:
            Opaque tenant identifier — the ONLY value that should be
            persisted in your PostgreSQL database.
        envelope:
            Fully-validated credential envelope.

        Raises
        ------
        CredentialSaveError
            If the Infisical write operation fails after the upsert attempt.
        """
        secret_name = _secret_name(integration_id)
        secret_value = envelope.model_dump_json()

        logger.info(
            "Saving credentials for integration_id='%s' (crm_type=%s, secret=%s).",
            integration_id,
            envelope.crm_type,
            secret_name,
        )

        try:
            self._upsert_secret(secret_name, secret_value)
        except Exception as exc:
            raise CredentialSaveError(integration_id, str(exc)) from exc

        logger.info(
            "Credentials saved successfully for integration_id='%s'.",
            integration_id,
        )

    # ------------------------------------------------------------------
    # READ FLOW
    # ------------------------------------------------------------------

    def get_credentials(self, integration_id: str) -> CrmCredentialEnvelope:
        """
        Retrieve and deserialise credentials for *integration_id*.

        Parameters
        ----------
        integration_id:
            The tenant reference stored in PostgreSQL.

        Returns
        -------
        CrmCredentialEnvelope
            Fully-validated envelope.  Call ``.to_credential_dict()`` for the
            plaintext dict to pass into ``BaseCrmClient``.

        Raises
        ------
        CredentialNotFoundError
            If Infisical has no secret for this integration_id.
        CredentialDecodeError
            If the stored JSON is malformed or fails Pydantic validation.
        """
        secret_name = _secret_name(integration_id)
        logger.debug(
            "Fetching credentials for integration_id='%s' (secret=%s).",
            integration_id,
            secret_name,
        )

        raw_value = self._fetch_secret(secret_name, integration_id)
        return self._deserialise(raw_value, integration_id)

    # ------------------------------------------------------------------
    # DELETE FLOW
    # ------------------------------------------------------------------

    def delete_credentials(self, integration_id: str) -> None:
        """
        Remove the Infisical secret for *integration_id*.

        Called when an integration is de-provisioned.  Idempotent — if the
        secret does not exist, this is a no-op (logs a warning).

        Raises
        ------
        CredentialDeleteError
            If the Infisical delete operation returns an unexpected error.
        """
        secret_name = _secret_name(integration_id)
        logger.info(
            "Deleting credentials for integration_id='%s' (secret=%s).",
            integration_id,
            secret_name,
        )

        try:
            from infisical_client import DeleteSecretOptions  # type: ignore[import]

            self._client.deleteSecret(
                options=DeleteSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_path=self._settings.secret_path,
                    secret_name=secret_name,
                )
            )
        except Exception as exc:
            msg = str(exc)
            # Treat "secret not found" on delete as a non-fatal warning
            if "not found" in msg.lower() or "does not exist" in msg.lower():
                logger.warning(
                    "Attempted to delete non-existent secret '%s' — ignoring.",
                    secret_name,
                )
                return
            raise CredentialDeleteError(integration_id, msg) from exc

        logger.info(
            "Credentials deleted for integration_id='%s'.", integration_id
        )

    # ------------------------------------------------------------------
    # ROTATE FLOW
    # ------------------------------------------------------------------

    def rotate_credentials(
        self,
        integration_id: str,
        new_envelope: CrmCredentialEnvelope,
    ) -> CrmCredentialEnvelope:
        """
        Replace existing credentials and verify the write succeeded.

        This is a save followed by an immediate read-back.  The read-back
        acts as a confirmation that the secret is retrievable with the new
        value.  If the read-back fails, the CredentialDecodeError or
        CredentialNotFoundError propagates unchanged so the caller can alert.

        Returns
        -------
        CrmCredentialEnvelope
            The freshly retrieved envelope (guaranteed to match *new_envelope*).
        """
        logger.info(
            "Rotating credentials for integration_id='%s'.", integration_id
        )
        self.save_credentials(integration_id, new_envelope)
        confirmed = self.get_credentials(integration_id)
        logger.info(
            "Credential rotation confirmed for integration_id='%s'.",
            integration_id,
        )
        return confirmed

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    def credentials_exist(self, integration_id: str) -> bool:
        """
        Return True if Infisical holds a secret for *integration_id*.
        Never raises — exceptions are caught and return False.
        """
        try:
            self.get_credentials(integration_id)
            return True
        except (CredentialNotFoundError, CredentialDecodeError):
            return False
        except Exception:
            logger.exception(
                "Unexpected error checking existence of credentials for '%s'.",
                integration_id,
            )
            return False

    # ------------------------------------------------------------------
    # Private SDK helpers
    # ------------------------------------------------------------------

    def _upsert_secret(self, secret_name: str, secret_value: str) -> None:
        """
        Create a new Infisical secret, or update it if it already exists.

        The Infisical SDK does not expose a native upsert, so we try
        ``createSecret`` first and fall back to ``updateSecret`` on conflict.
        """
        from infisical_client import (          # type: ignore[import]
            CreateSecretOptions,
            UpdateSecretOptions,
        )

        create_opts = CreateSecretOptions(
            project_id=self._settings.project_id,
            environment=self._settings.environment,
            secret_path=self._settings.secret_path,
            secret_name=secret_name,
            secret_value=secret_value,
        )

        try:
            self._client.createSecret(options=create_opts)
            logger.debug("Created new Infisical secret '%s'.", secret_name)
        except Exception as create_exc:
            create_msg = str(create_exc).lower()
            # Infisical returns a conflict-style error when the secret exists
            if "already exists" in create_msg or "conflict" in create_msg:
                logger.debug(
                    "Secret '%s' exists — updating instead.", secret_name
                )
                update_opts = UpdateSecretOptions(
                    project_id=self._settings.project_id,
                    environment=self._settings.environment,
                    secret_path=self._settings.secret_path,
                    secret_name=secret_name,
                    secret_value=secret_value,
                )
                self._client.updateSecret(options=update_opts)
                logger.debug("Updated Infisical secret '%s'.", secret_name)
            else:
                raise  # unexpected — propagate to save_credentials

    def _fetch_secret(self, secret_name: str, integration_id: str) -> str:
        """
        Retrieve the raw secret value string from Infisical.

        Raises
        ------
        CredentialNotFoundError
            When the SDK signals the secret does not exist.
        """
        from infisical_client import GetSecretOptions  # type: ignore[import]

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
                raise CredentialNotFoundError(integration_id) from exc
            raise  # unexpected SDK/network error

        # The SDK returns an object with a .secretValue attribute
        value: Optional[str] = getattr(secret, "secretValue", None)
        if value is None:
            raise CredentialNotFoundError(integration_id)
        return value

    def _deserialise(
        self, raw_value: str, integration_id: str
    ) -> CrmCredentialEnvelope:
        """
        Parse *raw_value* JSON string back into a CrmCredentialEnvelope.

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
        except ValidationError as exc:
            raise CredentialDecodeError(
                integration_id,
                f"Pydantic validation failed: {exc}",
            ) from exc
            
            