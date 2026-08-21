"""Legacy import shim for the moved Session Service."""

from ftre.services.session.compat import (
    ForkResult,
    RequestAdmission,
    SessionManager,
    SessionService,
)

__all__ = ["ForkResult", "RequestAdmission", "SessionManager", "SessionService"]
