"""Compatibility names for callers that still use the old Session vocabulary."""

from .service import ForkResult, RequestAdmission, SessionService

SessionManager = SessionService

__all__ = ["ForkResult", "RequestAdmission", "SessionManager", "SessionService"]
