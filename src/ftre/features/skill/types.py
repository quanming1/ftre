from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillRecord:
    name: str
    content: str
    owner: str
    source: str
    priority: int
    scope: str

