from __future__ import annotations
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field


class AgentBase(BaseModel):
    name: str = Field(..., max_length=200, description="Agent display name")
    email: EmailStr | None = Field(default=None, description="Email of the agent")
    is_active: bool = Field(default=True, description="Whether the agent is active")


class AgentCreate(AgentBase):
    crm_agent_id: str = Field(..., max_length=50, description="Agent ID in source CRM")
    source_system_id: int = Field(..., description="FK to source_systems table")


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = Field(default=None)
    is_active: bool | None = Field(default=None)


class AgentResponse(AgentBase):
    id: UUID
    tenant_id: UUID
    crm_agent_id: str
    source_system: str
    model_config = {"from_attributes": True}


class AgentBriefResponse(BaseModel):
    """Lightweight agent info — used when nested inside ticket responses."""

    id: UUID
    name: str
    email: EmailStr | None
    model_config = {"from_attributes": True}
