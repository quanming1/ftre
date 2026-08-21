from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptAssemblyReceipt:
    agent_id: str
    session_id: str
    sections: tuple[dict[str, Any], ...]
    total_bytes: int
    token_estimate: int

    def as_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "session_id": self.session_id, "sections": [dict(item) for item in self.sections], "total_bytes": self.total_bytes, "token_estimate": self.token_estimate}

