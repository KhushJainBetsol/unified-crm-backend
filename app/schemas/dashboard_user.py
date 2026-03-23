"""
Pydantic schemas for the dashboard_users table.
Security rules enforced here:
  - password_hash is NEVER included in any response schema
  - plain password is ONLY accepted in Create / login request schemas
  - UpdatePassword is a dedicated schema to change password safely
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class DashboardUserCreate(BaseModel):
    email: EmailStr = Field(..., description="Unique login email")
    password: str = Field(
        ...,
        min_length=8,
        description="Plain password — will be hashed before storage",
    )

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class DashboardUserUpdate(BaseModel):
    email: EmailStr | None = Field(default=None)


class UpdatePassword(BaseModel):
    current_password: str = Field(..., description="Current password for verification")
    new_password: str = Field(..., min_length=8)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class DashboardUserResponse(BaseModel):
    """Safe response — never exposes password_hash."""
    id: UUID
    email: EmailStr
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str
