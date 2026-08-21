from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str | None = None
    factory: Callable[[dict[str, Any]], str] | None = None
    priority: int = 100
    scope: str = "global"
    required: bool = False
    owner: str = "system"
    source: str = "builtin"


@dataclass(frozen=True, slots=True)
class PromptContribution:
    """One rendered, immutable section in a PromptAssembly."""

    name: str
    content: str
    owner: str
    source: str
    scope: str
    order: int


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    """Complete prompt input passed through ``system-prompt/assemble``."""

    agent_id: str
    session_id: str
    workspace: str
    contributions: tuple[PromptContribution, ...]
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "text": self.text,
            "contributions": [
                {
                    "name": item.name,
                    "content": item.content,
                    "owner": item.owner,
                    "source": item.source,
                    "scope": item.scope,
                    "order": item.order,
                }
                for item in self.contributions
            ],
        }
