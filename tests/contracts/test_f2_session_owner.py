from __future__ import annotations

from ftre.services.session import SessionService
from ftre.services.session.service import ForkResult, RequestAdmission
from ftre.session import SessionManager
from ftre.session.manager import SessionService as LegacySessionService


def test_session_service_is_the_single_runtime_owner() -> None:
    assert SessionService.key == "sessions"
    assert SessionManager is SessionService
    assert LegacySessionService is SessionService
    assert ForkResult.__module__ == "ftre.services.session.service"
    assert RequestAdmission.__module__ == "ftre.services.session.service"
