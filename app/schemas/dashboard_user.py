# """
# Pydantic schemas for the dashboard_users table.
# Security rules enforced here:
#   - password_hash is NEVER included in any response schema
#   - plain password is ONLY accepted in Create / login request schemas
#   - UpdatePassword is a dedicated schema to change password safely
# """

# from __future__ import annotations

# from datetime import datetime
# from uuid import UUID

# from pydantic import BaseModel, EmailStr, Field, field_validator


# class DashboardUserCreate(BaseModel):
#     email: EmailStr = Field(..., description="Unique login email")
#     password: str = Field(
#         ...,
#         min_length=8,
#         description="Plain password — will be hashed before storage",
#     )

#     @field_validator("password")
#     @classmethod
#     def _password_strength(cls, v: str) -> str:
#         if not any(c.isupper() for c in v):
#             raise ValueError("Password must contain at least one uppercase letter")
#         if not any(c.isdigit() for c in v):
#             raise ValueError("Password must contain at least one digit")
#         return v


# class DashboardUserUpdate(BaseModel):
#     email: EmailStr | None = Field(default=None)


# class UpdatePassword(BaseModel):
#     current_password: str = Field(..., description="Current password for verification")
#     new_password: str = Field(..., min_length=8)

#     @field_validator("new_password")
#     @classmethod
#     def _password_strength(cls, v: str) -> str:
#         if not any(c.isupper() for c in v):
#             raise ValueError("Password must contain at least one uppercase letter")
#         if not any(c.isdigit() for c in v):
#             raise ValueError("Password must contain at least one digit")
#         return v


# class DashboardUserResponse(BaseModel):
#     """Safe response — never exposes password_hash."""
#     id: UUID
#     email: EmailStr
#     created_at: datetime
#     updated_at: datetime

#     model_config = {"from_attributes": True}


# # ---------------------------------------------------------------------------
# # Auth schemas
# # ---------------------------------------------------------------------------
# class LoginRequest(BaseModel):
#     email: EmailStr
#     password: str = Field(..., min_length=1)


# class TokenResponse(BaseModel):
#     access_token: str
#     refresh_token: str
#     token_type: str = "bearer"


# class RefreshTokenRequest(BaseModel):
#     refresh_token: str

"""
Pydantic schemas for the dashboard_users table.
Authentication is handled by Keycloak — no password fields here.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class DashboardUserCreate(BaseModel):
    tenant_id: UUID
    keycloak_sub: str = Field(..., min_length=1, description="Keycloak user subject ID")
    email: EmailStr
    role: str = Field(..., description="User role: admin, agent, or superadmin")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        valid_roles = {"admin", "agent", "superadmin"}
        if v not in valid_roles:
            raise ValueError(f"role must be one of {valid_roles}, got '{v}'")
        return v


class DashboardUserUpdate(BaseModel):
    email: EmailStr | None = None
    role: str | None = Field(default=None, description="User role: admin, agent, or superadmin")
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str | None) -> str | None:
        if v is None:
            return None
        valid_roles = {"admin", "agent", "superadmin"}
        if v not in valid_roles:
            raise ValueError(f"role must be one of {valid_roles}, got '{v}'")
        return v


class DashboardUserResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    keycloak_sub: str
    email: EmailStr
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Auth schemas — token issued by Keycloak, validated by backend
# ---------------------------------------------------------------------------
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str

