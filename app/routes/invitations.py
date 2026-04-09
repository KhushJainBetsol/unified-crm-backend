"""
app/routes/invitations.py

Invitation APIs:
  GET  /invitations/validate    → validate an invite token
  POST /invitations/accept      → accept an invite and activate account
  POST /invitations/invite-agent → admin invites an agent to their tenant
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.dependencies import get_db
from app.services.invitation_service import (
    svc_accept_invite,
    svc_invite_agent,
    svc_validate_invite,
)

router = APIRouter(prefix="/invitations", tags=["Invitations"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ValidateInviteResponse(BaseModel):
    email: str
    role: str
    tenant_name: str
    tenant_id: str
    realm_name: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class InviteAgentRequest(BaseModel):
    email: EmailStr
    role: str = "agent"
    first_name: str = ""
    last_name: str = ""
    name: str = ""


# ---------------------------------------------------------------------------
# GET /invitations/validate
# ---------------------------------------------------------------------------

@router.get("/validate", response_model=ValidateInviteResponse)
async def validate_invite(token: str, db: AsyncSession = Depends(get_db)):
    """Validate an invite token and return invite metadata."""
    return await svc_validate_invite(db, token)


# ---------------------------------------------------------------------------
# POST /invitations/accept
# ---------------------------------------------------------------------------

@router.post("/accept")
async def accept_invite(body: AcceptInviteRequest, db: AsyncSession = Depends(get_db)):
    """Accept an invite and activate the user account."""
    return await svc_accept_invite(db, token=body.token, password=body.password)


# ---------------------------------------------------------------------------
# POST /invitations/invite-agent
# ---------------------------------------------------------------------------

@router.post("/invite-agent", status_code=status.HTTP_201_CREATED)
async def invite_agent(
    body: InviteAgentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    """Admin invites an agent to their tenant."""
    return await svc_invite_agent(
        db=db,
        current_user=current_user,
        email=body.email,
        role=body.role,
        first_name=body.first_name,
        last_name=body.last_name,
    )