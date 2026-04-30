from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


class AuthTypeOptionSchema(BaseModel):
    """
    Metadata for a single authentication strategy option.
    """
    value: str = Field(
        ...,
        description="Authentication strategy identifier",
        examples=["api_token", "oauth2", "basic_auth", "api_key", "hmac"]
    )
    label: str = Field(
        ...,
        description="Human-readable label for the UI dropdown/radio group",
        examples=["API Token", "OAuth2", "Basic Auth", "API Key", "HMAC"]
    )
    icon: str = Field(
        ...,
        description="Single character icon/badge displayed next to label",
        examples=["T", "O", "U", "K", "H"]
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "value": "api_token",
            "label": "API Token",
            "icon": "T"
        }
    })


class CrmAuthStrategySchema(BaseModel):
    """
    Metadata about a CRM's authentication strategy.
    """
    strategy: str = Field(
        ...,
        description="Authentication strategy type: 'api_token', 'oauth2', or 'basic'",
        examples=["api_token", "oauth2", "basic"]
    )
    token_header: str = Field(
        ...,
        description="HTTP header name where token/credentials are injected",
        examples=["Authorization", "X-Api-Key"]
    )
    token_prefix: str = Field(
        ...,
        description="Prefix prepended to token value in the header",
        examples=["Token token=", "Bearer ", ""]
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "strategy": "api_token",
            "token_header": "Authorization",
            "token_prefix": "Token token="
        }
    })


class CrmInfoSchema(BaseModel):
    """
    Complete metadata for a single supported CRM adapter.
    """
    crm_key: str = Field(
        ...,
        description="Unique registry key for this CRM",
        examples=["zammad", "espocrm"]
    )
    display_name: str = Field(
        ...,
        description="Human-readable CRM name for UI display",
        examples=["Zammad", "EspoCRM"]
    )
    description: Optional[str] = Field(
        ...,
        description="Short description of the CRM system",
        examples=["Open-source helpdesk & ticketing", "Self-hosted open-source CRM"]
    )
    default_base_url: Optional[str] = Field(
        ...,
        description="Example/template base URL for this CRM",
        examples=["https://support.yourcompany.com", "https://crm.yourcompany.com"]
    )
    primary_auth_strategy: CrmAuthStrategySchema = Field(
        ...,
        description="Primary authentication strategy from adapter config"
    )
    supported_auth_options: List[AuthTypeOptionSchema] = Field(
        ...,
        description="All authentication method options available for this CRM"
    )
    supported_capabilities: List[str] = Field(
        ...,
        description="List of data operations this CRM supports",
        examples=[["fetch_tickets", "fetch_agents", "fetch_ticket_by_id", "fetch_organizations"]]
    )
    webhook_model: Optional[str] = Field(
        ...,
        description="Webhook configuration model: 'shared' or 'per_event'",
        examples=["shared", "per_event"]
    )
    webhook_instructions: List[str] = Field(
        ...,
        description="Step-by-step instructions for configuring webhooks"
    )
    auth_instructions: Dict[str, List[str]] = Field(
        ...,
        description="Auth-type-specific setup instructions"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "crm_key": "zammad",
            "display_name": "Zammad",
            "description": "Open-source helpdesk & ticketing",
            "default_base_url": "https://support.yourcompany.com",
            "primary_auth_strategy": {
                "strategy": "api_token",
                "token_header": "Authorization",
                "token_prefix": "Token token="
            },
            "supported_auth_options": [
                {"value": "api_token", "label": "API Token", "icon": "T"}
            ],
            "supported_capabilities": ["fetch_tickets"],
            "webhook_model": "shared",
            "webhook_instructions": ["Go to Admin...", "Click Add..."],
            "auth_instructions": {
                "api_token": ["Log in...", "Generate key..."]
            }
        }
    })


class SupportedCrmsResponse(BaseModel):
    """
    Response body for GET /api/v1/config/crms.
    """
    crms: List[CrmInfoSchema] = Field(
        ...,
        description="List of all supported CRM adapters"
    )
    total: int = Field(
        ...,
        description="Total number of supported CRMs"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "crms": [
                {
                    "crm_key": "zammad",
                    "display_name": "Zammad",
                    "description": "Open-source helpdesk & ticketing",
                    "default_base_url": "https://support.yourcompany.com",
                    "primary_auth_strategy": {
                        "strategy": "api_token",
                        "token_header": "Authorization",
                        "token_prefix": "Token token="
                    },
                    "supported_auth_options": [
                        {"value": "api_token", "label": "API Token", "icon": "T"}
                    ],
                    "supported_capabilities": ["fetch_tickets"],
                    "webhook_model": "shared",
                    "webhook_instructions": [], # Fixed: Changed from [...]
                    "auth_instructions": {}      # Fixed: Changed from {...}
                }
            ],
            "total": 1
        }
    })