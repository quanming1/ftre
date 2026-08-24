from __future__ import annotations

from ftre.services.session import SessionService
from ftre.services.session.service import ForkResult


def test_session_service_is_the_single_runtime_owner() -> None:
    assert SessionService.key == "sessions"
    assert ForkResult.__module__ == "ftre.services.session.service"
    assert not hasattr(SessionService, "admit_inbound")
