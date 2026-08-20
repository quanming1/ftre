from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiDependencies:
    """Public dependency bundle used by compatibility routers.

    New routers capture individual Service handles.  This bundle exists only
    for the legacy route surface while it is being split by Owner.
    """

    sessions: Any | None = None
    agents: Any | None = None
    agent_profiles: Any | None = None
    commands: Any | None = None
    config: Any | None = None
    traces: Any | None = None

