# crm/config/models.py
"""
Pydantic v2 models that represent a fully-parsed, strongly-typed CRM adapter
configuration.  ConfigLoader deserialises raw YAML into these models so that
the rest of the system works with validated, auto-completed objects rather than
raw dicts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# HTTP / Transport layer
# ---------------------------------------------------------------------------

class HttpConfig(BaseModel):
    timeout_seconds: int = 30
    max_retries: int = 3
    backoff_base: float = 2.0
    retry_on_status: List[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class AuthConfig(BaseModel):
    strategy: str  # "api_token" | "oauth2" | "basic"
    token_header: Optional[str] = "Authorization"
    token_prefix: Optional[str] = ""

    @field_validator("strategy")
    @classmethod
    def strategy_must_be_known(cls, v: str) -> str:
        allowed = {"api_token", "oauth2", "basic"}
        if v not in allowed:
            raise ValueError(f"auth.strategy must be one of {allowed}, got '{v}'")
        return v


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class PaginationConfig(BaseModel):
    strategy: str = "page"           # "page" | "cursor" | "offset"
    page_param: str = "page"
    per_page_param: str = "per_page"
    default_page_size: int = 100
    items_path: Optional[str] = None  # JSONPath into response body; None = root list
    total_count_path: Optional[str] = None

    @field_validator("strategy")
    @classmethod
    def strategy_must_be_known(cls, v: str) -> str:
        allowed = {"page", "cursor", "offset"}
        if v not in allowed:
            raise ValueError(f"pagination.strategy must be one of {allowed}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class EndpointConfig(BaseModel):
    path: str
    method: str = "GET"
    default_params: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Field Mappings & Value Maps
# ---------------------------------------------------------------------------

class FieldMappingConfig(BaseModel):
    """
    Maps unified domain field names to dot-notation paths inside the raw
    CRM response payload.  A leading "?" on the path marks the field as
    optional — a missing key will resolve to None instead of raising.
    """
    ticket: Dict[str, str] = Field(default_factory=dict)
    agent: Dict[str, str] = Field(default_factory=dict)
    organization: Dict[str, str] = Field(default_factory=dict)
    customer: Dict[str, str] = Field(default_factory=dict)

# ---------------------------------------------------------------------------
# Top-level adapter config
# ---------------------------------------------------------------------------

class AdapterConfig(BaseModel):
    """
    Fully validated configuration for a single CRM adapter instance.
    Parsed from e.g. config/zammad/config.yaml by ConfigLoader.
    """
    auth: AuthConfig
    http: HttpConfig = Field(default_factory=HttpConfig)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    endpoints: Dict[str, EndpointConfig] = Field(default_factory=dict)
    field_mappings: FieldMappingConfig = Field(default_factory=FieldMappingConfig)

    # Optional normalisation look-up tables defined in the YAML
    status_map: Dict[str, str] = Field(default_factory=dict)
    priority_map: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def endpoints_not_empty(self) -> "AdapterConfig":
        if not self.endpoints:
            raise ValueError("AdapterConfig must declare at least one endpoint.")
        return self


# ---------------------------------------------------------------------------
# Frontend Auth Type Metadata
# ---------------------------------------------------------------------------

class AuthTypeMetadata(BaseModel):
    """Metadata for a single authentication strategy supported by a CRM."""
    value: str = Field(..., description="Auth strategy identifier (e.g., 'api_token', 'oauth2')")
    label: str = Field(..., description="Human-readable label for UI display")
    icon: str = Field(..., description="Single character icon/badge for UI")


# ---------------------------------------------------------------------------
# Master registry entry (from crm_adapters.yaml)
# ---------------------------------------------------------------------------

class AdapterRegistryEntry(BaseModel):
    """One entry in crm_adapters.yaml under the 'adapters' key."""
    display_name: str = Field(..., description="Human-readable name for UI display")
    config_path: str = Field(..., description="Path to adapter config YAML relative to config base dir")
    adapter_class: str = Field(..., description="Fully-qualified importable class path")
    description: Optional[str] = Field(default=None, description="Short description of the CRM")
    default_base_url: Optional[str] = Field(default=None, description="Example base URL for this CRM")
    supported_capabilities: List[str] = Field(default_factory=list)
    supported_auth_types: List[AuthTypeMetadata] = Field(default_factory=list, description="Available auth strategies")
    webhook_model: Optional[str] = Field(default=None, description="Webhook model: 'shared' or 'per_event'")
    webhook_instructions: List[str] = Field(default_factory=list, description="Step-by-step webhook setup instructions")
    auth_instructions: Dict[str, List[str]] = Field(default_factory=dict, description="Auth-type-specific instructions")


class AdapterRegistryManifest(BaseModel):
    """The entire crm_adapters.yaml file deserialised."""
    adapters: Dict[str, AdapterRegistryEntry]