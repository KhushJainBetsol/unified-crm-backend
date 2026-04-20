"""
app/credentials/models.py
===========================
Configuration models for the credential management layer.
"""

from __future__ import annotations

import os
from pydantic import BaseModel, Field
from app.credentials.exceptions import InfisicalConfigError


class CrmCredentialEnvelope(BaseModel):
    """
    A unified wrapper for CRM credentials, ensuring the factory 
    receives a consistent data structure regardless of the CRM provider.
    """
    crm_type: str
    base_url: str
    credentials: dict
    
class InfisicalSettings(BaseModel):
    """
    Configuration for the Infisical SDK client.
    
    Values are ideally mapped from the central Pydantic settings object
    at startup to ensure consistency between .env and the manager.
    """
    client_id: str = Field(..., min_length=1)
    client_secret: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    environment: str = Field(default="prod")
    host: str = Field(default="https://app.infisical.com")
    secret_path: str = Field(default="/crm")

    @classmethod
    def from_env(cls) -> "InfisicalSettings":
        """
        Construct directly from environment variables.
        Used as a fallback if the central settings object isn't used.
        """
        required_keys = {
            "client_id": "INFISICAL_CLIENT_ID",
            "client_secret": "INFISICAL_CLIENT_SECRET",
            "project_id": "INFISICAL_PROJECT_ID",
        }
        
        # Capture and strip values to handle RHEL/Shell formatting quirks
        values = {k: os.getenv(v, "").strip() for k, v in required_keys.items()}
        
        missing = [v for k, v in required_keys.items() if not values[k]]
        
        if missing:
            raise InfisicalConfigError(
                f"Missing or empty environment variables: {missing}. "
                "Ensure these are exported in your current shell session."
            )

        return cls(
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            project_id=values["project_id"],
            environment=os.getenv("INFISICAL_ENVIRONMENT", "prod"),
            host=os.getenv("INFISICAL_SITE_URL") or os.getenv("INFISICAL_HOST", "https://app.infisical.com"),
            secret_path=os.getenv("INFISICAL_SECRET_PATH", "/crm"),
        )