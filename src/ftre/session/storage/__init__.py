"""Legacy re-export for Session persistence adapters."""

from ftre.services.session.persistence.json_store import *
from ftre.services.session.persistence.repository import SessionRepository

__all__ = ["SessionRepository"]
