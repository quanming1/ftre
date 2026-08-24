"""System Prompt Service 的 section、贡献和最终组装模型。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptSection:
    """由 Plugin 注册的 prompt 来源，可按 priority/scope 参与组装。"""
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
    """PromptAssembly 中已经渲染完成的一段不可变 section。"""

    name: str
    content: str
    owner: str
    source: str
    scope: str
    order: int


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    """经 ``system-prompt/assemble`` Hook 传递的完整 prompt 输入。"""

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
