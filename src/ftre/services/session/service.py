"""Session Service boundary.

The temporary subclass keeps the mature persistence implementation intact while
giving Composition and Features one stable ``sessions`` key.
"""

from ftre.session.manager import SessionManager


class SessionService(SessionManager):
    """Compatibility-backed public Session provider."""
    key = "sessions"
