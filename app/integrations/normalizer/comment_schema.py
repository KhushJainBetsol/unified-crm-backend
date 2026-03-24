"""
app/integrations/normalizer/comment_schema.py

Dataclass that represents ONE comment/article/note after normalization.

Both EspoCRM and Zammad raw dicts are converted into this before
being handed to the comment sync service and repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NormalizedComment:
    """
    CRM-agnostic comment ready for DB upsert.

    Fields map 1-to-1 to ticket_comments table columns.
    """

    # The comment's own ID inside the originating CRM (str — Zammad int, EspoCRM UUID)
    crm_comment_id: str

    # Body text of the comment (may contain HTML from Zammad)
    body: str | None

    # Type label from the CRM: "note", "email", "Post", "web", "phone" …
    comment_type: str | None

    # Who wrote it
    author_name: str | None
    author_email: str | None

    # True = internal note visible only to agents
    is_internal: bool

    # Timestamps as reported by the CRM
    crm_created_at: datetime | None
    crm_updated_at: datetime | None

    # Which CRM this came from — "zammad" | "espocrm"
    source_system: str = field(default="")
