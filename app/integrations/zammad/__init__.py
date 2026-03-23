"""
app/integrations/zammad/__init__.py
"""

from app.integrations.zammad.client import ZammadClient, ZammadClientError, ZammadAuthError
from app.integrations.zammad.service import ZammadService

__all__ = [
    "ZammadClient",
    "ZammadClientError",
    "ZammadAuthError",
    "ZammadService",
]
