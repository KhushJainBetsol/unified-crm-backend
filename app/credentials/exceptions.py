# crm/credentials/exceptions.py
"""
Typed exception hierarchy for the credential management layer.

Keeping exceptions in their own module avoids circular imports — every other
module in this layer can import from here without pulling in the full manager.
"""

from __future__ import annotations


class CredentialError(Exception):
    """
    Base class for all credential-layer errors.
    Catch this if you want a single handler for any credential problem.
    """


class CredentialNotFoundError(CredentialError):
    """
    Raised when Infisical has no secret stored for a given integration_id.
    This is a recoverable state — the integration may simply not be
    provisioned yet.
    """

    def __init__(self, integration_id: str) -> None:
        self.integration_id = integration_id
        super().__init__(
            f"No credentials found in Infisical for integration_id='{integration_id}'. "
            "Ensure the integration has been provisioned via save_credentials()."
        )


class CredentialSaveError(CredentialError):
    """
    Raised when Infisical rejects a create/update secret operation.
    Wraps the underlying SDK or network error as __cause__.
    """

    def __init__(self, integration_id: str, reason: str) -> None:
        self.integration_id = integration_id
        super().__init__(
            f"Failed to save credentials for integration_id='{integration_id}': {reason}"
        )


class CredentialDeleteError(CredentialError):
    """
    Raised when an Infisical delete secret operation fails.
    """

    def __init__(self, integration_id: str, reason: str) -> None:
        self.integration_id = integration_id
        super().__init__(
            f"Failed to delete credentials for integration_id='{integration_id}': {reason}"
        )


class CredentialDecodeError(CredentialError):
    """
    Raised when the secret value retrieved from Infisical cannot be parsed
    back into a valid credential dictionary (e.g. corrupted JSON, wrong schema).
    """

    def __init__(self, integration_id: str, reason: str) -> None:
        self.integration_id = integration_id
        super().__init__(
            f"Failed to decode credentials for integration_id='{integration_id}': {reason}. "
            "The secret may be corrupted — consider re-provisioning via save_credentials()."
        )


class InfisicalConfigError(CredentialError):
    """
    Raised at startup when the InfisicalCredentialManager cannot be
    initialised due to missing or invalid configuration (missing env vars,
    unreachable host, bad Machine Identity credentials, etc.).
    """