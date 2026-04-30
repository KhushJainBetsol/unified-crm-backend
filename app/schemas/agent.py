from __future__ import annotations
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, field_validator


def _empty_str_to_none(v: str | None) -> str | None:
    """Coerce empty / whitespace-only strings to None before EmailStr validation."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


class AgentBase(BaseModel):
    name: str = Field(..., max_length=200, description="Agent display name")
    email: EmailStr | None = Field(default=None, description="Email of the agent")
    is_active: bool = Field(default=True, description="Whether the agent is active")

    @field_validator("email", mode="before")
    @classmethod
    def normalise_email(cls, v: str | None) -> str | None:
        return _empty_str_to_none(v)


class AgentCreate(AgentBase):
    crm_agent_id: str = Field(..., max_length=50, description="Agent ID in source CRM")
    source_system_id: int = Field(..., description="FK to source_systems table")


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = Field(default=None)
    is_active: bool | None = Field(default=None)

    @field_validator("email", mode="before")
    @classmethod
    def normalise_email(cls, v: str | None) -> str | None:
        return _empty_str_to_none(v)


class AgentResponse(AgentBase):
    id: UUID
    tenant_id: UUID
    crm_agent_id: str
    source_system: str
    invitation_status: str | None = None
    model_config = {"from_attributes": True}


class AgentBriefResponse(BaseModel):
    """Lightweight agent info — used when nested inside ticket responses."""
    id: UUID
    name: str
    email: EmailStr | None
    model_config = {"from_attributes": True}

    @field_validator("email", mode="before")
    @classmethod
    def normalise_email(cls, v: str | None) -> str | None:
        return _empty_str_to_none(v)