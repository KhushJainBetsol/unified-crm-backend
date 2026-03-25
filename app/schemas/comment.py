"""
app/schemas/comment.py

Pydantic schemas for ticket comment API responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class CommentResponse(BaseModel):
    """
    Single comment as returned by the API.
    """

    id: uuid.UUID
    ticket_id: uuid.UUID
    source_system: str          # "zammad" | "espocrm"
    crm_comment_id: str

    body: str | None
    comment_type: str | None    # "note", "email", "Post", "phone" …
    author_name: str | None
    author_email: str | None
    is_internal: bool

    crm_created_at: datetime | None
    crm_updated_at: datetime | None

    model_config = {"from_attributes": True}
