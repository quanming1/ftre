"""Public facade over persisted Agent profile definitions."""

from __future__ import annotations

from typing import Any

from .manager import AgentManager
from .models import EffectiveProfile


class AgentProfileService:
    """Expose profile CRUD without leaking AgentManager internals to Features."""
    key = "agent_profiles"

    def __init__(self, manager: AgentManager) -> None:
        self.manager = manager

    def list(self) -> list[dict[str, Any]]:
        return self.manager.list_agents()

    def get(self, agent_id: str):
        return self.manager.load(agent_id)

    def create(self, **kwargs: Any):
        return self.manager.create_agent_profile(**kwargs)

    def update(self, agent_id: str, payload: dict[str, Any]):
        return self.manager.update_agent(agent_id, payload)

    def delete(self, agent_id: str) -> None:
        self.manager.delete_agent(agent_id)

    def resolve(self, agent_id: str, session_id: str | None = None) -> EffectiveProfile:
        return EffectiveProfile(agent_id, self.manager.load(agent_id))

    def list_prompts(self, agent_id: str) -> dict[str, str]:
        """Read prompt files through the profile owner."""
        return self.manager.read_prompts(agent_id)

    def update_prompt(self, agent_id: str, filename: str, content: str) -> None:
        """Write one allow-listed prompt file through the profile owner."""
        self.manager.write_prompt(agent_id, filename, content)
