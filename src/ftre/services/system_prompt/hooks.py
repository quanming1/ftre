"""Structured system-prompt assembly Hook contract."""
# 中文说明：System Prompt assemble Hook 契约：传递结构化 PromptAssembly 和 receipt，不让监听器修改 Service 内部列表。

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ftre_agent import AgentSubject

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

from .types import PromptAssembly

SYSTEM_PROMPT_ASSEMBLE = "system-prompt/assemble"


@dataclass(frozen=True, slots=True)
class PromptAssemblyPayload:
    """Read-only assembly context; listeners replace the assembly, not messages."""

    agent: AgentSubject
    session_id: str
    workspace: str
    assembly: PromptAssembly
    messages: tuple[Any, ...]
    inbound_data: Mapping[str, Any]
    config: Any
    event_loop: Any
    cancellation: asyncio.Event
    profile: Any = None
    channel_id: str = ""


async def _accept(payload: PromptAssemblyPayload) -> PromptAssembly:
    return payload.assembly


SYSTEM_PROMPT_ASSEMBLE_SPEC = HookSpec(
    SYSTEM_PROMPT_ASSEMBLE,
    "system-prompt",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=PromptAssemblyPayload,
    result_type=PromptAssembly,
    default=_accept,
    scope=HookScope.AGENT,
)


__all__ = ["SYSTEM_PROMPT_ASSEMBLE_SPEC", "PromptAssemblyPayload"]
