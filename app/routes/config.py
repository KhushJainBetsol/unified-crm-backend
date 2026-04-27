"""
app/routes/config.py

REST API for retrieving CRM configuration metadata.

Endpoints
---------
GET /api/v1/config/crms — fetch all supported CRM adapters with complete metadata

Auth
----
All endpoints require a valid Keycloak JWT. tenant_id is extracted from JWT claims.

Purpose
-------
Allows the frontend to dynamically discover:
- All supported CRM systems
- Authentication strategy options and instructions
- Webhook configuration models and setup steps
- Supported data operations for each CRM
- Example URLs and credential gathering steps

This eliminates the need for hardcoded CRM configurations in the frontend.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.adapter_dependencies.deps import get_adapter_registry
from app.config.registry import AdapterRegistry
from app.core.auth import CurrentUser, get_current_user
from app.schemas.config import (
    AuthTypeOptionSchema,
    CrmAuthStrategySchema,
    CrmInfoSchema,
    SupportedCrmsResponse,
)

router = APIRouter(prefix="/config", tags=["Configuration"])
logger = logging.getLogger(__name__)


@router.get(
    "/crms",
    response_model=SupportedCrmsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get all supported CRM adapters",
    responses={
        200: {"description": "List of all supported CRMs with metadata"},
        401: {"description": "Unauthorized - invalid or missing JWT"},
        503: {"description": "Service unavailable - adapter registry not initialized"},
    }
)
async def get_supported_crms(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    registry: Annotated[AdapterRegistry, Depends(get_adapter_registry)],
) -> SupportedCrmsResponse:
    """
    Retrieve metadata for all supported CRM adapters.

    This endpoint exposes complete CRM configuration to the frontend, including:
    - Display name and description for UI
    - Default base URL template
    - Primary authentication strategy details (header, prefix)
    - Available authentication method options with labels and icons
    - Supported data operations/capabilities
    - Webhook model (shared vs per-event) and setup instructions
    - Step-by-step instructions for each authentication type

    The frontend uses this information to:
    - Populate CRM selection dropdowns dynamically
    - Display CRM descriptions and options
    - Provide pre-filled URL templates
    - Build authentication forms (auth type options drive form structure)
    - Display step-by-step setup instructions for credentials
    - Configure webhook support with appropriate UI (shared vs per-event)
    - Show webhook setup instructions

    Query Parameters
    ----------------
    None — requires only JWT authentication

    Response
    --------
    SupportedCrmsResponse
        - crms: List of CRM metadata objects with all configuration
        - total: Count of available CRMs

    Raises
    ------
    HTTPException 401
        If JWT is missing, expired, or invalid.
    HTTPException 503
        If the adapter registry failed to initialize at startup.

    Example
    -------
    GET /api/v1/config/crms
    Authorization: Bearer <JWT>

    Response (200):
    {
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
                    {
                        "value": "api_token",
                        "label": "API Token",
                        "icon": "T"
                    },
                    {
                        "value": "basic_auth",
                        "label": "Basic Auth",
                        "icon": "U"
                    }
                ],
                "supported_capabilities": [
                    "fetch_tickets",
                    "fetch_agents",
                    "fetch_ticket_by_id",
                    "fetch_organizations"
                ],
                "webhook_model": "shared",
                "webhook_instructions": [
                    "Go to Admin → System → Webhooks.",
                    "Click 'Add Webhook'.",
                    "..."
                ],
                "auth_instructions": {
                    "api_token": [
                        "Log in to your Zammad instance.",
                        "Click your avatar at the top-right → select Profile.",
                        "..."
                    ],
                    "basic_auth": [
                        "No token creation needed.",
                        "..."
                    ]
                }
            }
        ],
        "total": 1
    }
    """
    logger.debug(
        "User %s (tenant_id=%s) requested CRM configuration",
        user.email,
        user.tenant_id,
    )

    # Get list of all adapter keys from registry
    adapter_keys = registry.list_adapter_keys()
    logger.debug("Available adapters: %s", adapter_keys)

    crm_list: list[CrmInfoSchema] = []

    # Build metadata for each adapter
    for crm_key in adapter_keys:
        try:
            entry = registry.get_entry(crm_key)
            config = registry.get_adapter_config(crm_key)

            # Extract primary auth strategy details
            primary_auth_strategy = CrmAuthStrategySchema(
                strategy=config.auth.strategy,
                token_header=config.auth.token_header or "Authorization",
                token_prefix=config.auth.token_prefix or "",
            )

            # Convert supported auth types to schema format
            supported_auth_options = [
                AuthTypeOptionSchema(
                    value=auth_type.value,
                    label=auth_type.label,
                    icon=auth_type.icon,
                )
                for auth_type in entry.supported_auth_types
            ]

            # Build CRM info object with all metadata
            crm_info = CrmInfoSchema(
                crm_key=crm_key,
                display_name=entry.display_name,
                description=entry.description,
                default_base_url=entry.default_base_url,
                primary_auth_strategy=primary_auth_strategy,
                supported_auth_options=supported_auth_options,
                supported_capabilities=entry.supported_capabilities,
                webhook_model=entry.webhook_model,
                webhook_instructions=entry.webhook_instructions,
                auth_instructions=entry.auth_instructions,
            )

            crm_list.append(crm_info)
            logger.debug(
                "Added CRM '%s' (%s) with %d auth options, %d capabilities",
                crm_key,
                entry.display_name,
                len(supported_auth_options),
                len(entry.supported_capabilities),
            )

        except Exception as e:
            logger.warning(
                "Failed to load metadata for adapter '%s': %s. Skipping.",
                crm_key,
                e,
            )
            # Continue to next adapter instead of failing entire request
            continue

    logger.info(
        "Returning metadata for %d CRMs to user %s",
        len(crm_list),
        user.email,
    )

    return SupportedCrmsResponse(crms=crm_list, total=len(crm_list))
