from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolRestriction:
    agent_id: str
    owner: str
    allow: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)

