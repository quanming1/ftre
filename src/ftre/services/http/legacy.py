"""Compatibility bridge for the pre-F1 aggregate API router."""

from __future__ import annotations

from typing import Any


def bind_legacy_api(*, sessions: Any, agents: Any, agent_profiles: Any, commands: Any, agent_loop: Any) -> None:
    """Bind one immutable runtime bundle for the old router during migration.

    The new Service routers do not call this function.  It exists so the
    existing Desktop HTTP/WebSocket surface can remain byte-compatible while
    its endpoints are moved one Owner at a time.
    """
    from ftre.api import routes

    routes.set_session_manager(sessions)
    routes.set_agent_manager(getattr(agent_profiles, "manager", agent_profiles))
    routes.set_command_manager(getattr(commands, "manager", commands))
    routes.set_agent_loop(agent_loop)
