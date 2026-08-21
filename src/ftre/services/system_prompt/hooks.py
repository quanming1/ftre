"""Structured system-prompt assembly Hook contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ftre.platform.hooks import (
    SYSTEM_PROMPT_ASSEMBLE,
    HookFailurePolicy,
    HookMode,
    HookScope,
    HookSpec,
)
from ftre.services.agent.hooks import AgentSubject

from .types import PromptAssembly


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
