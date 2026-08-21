"""AgentLoop Provider：唯一负责把公共 Service 组合成数据面 Loop。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ftre.services.agent_loop.runtime.loop.engine import AgentLoop

from .driver import AgentLoopDriver


@dataclass(frozen=True)
class AgentRuntimeServices:
    """构造 AgentLoop 所需的公开 Service 句柄。"""

    sessions: Any
    message_bus: Any
    channels: Any
    tools: Any
    commands: Any
    agent_profiles: Any
    event_hub: Any
    plugin_manager: Any
    agents: Any | None = None
    system_prompt: Any | None = None
    hook_runtime: Any | None = None
    compaction: Any | None = None


@dataclass
class AgentLoopRuntime:
    """Provider 创建的内部 Loop 与公开 Driver 配对。"""

    loop: AgentLoop
    driver: AgentLoopDriver


class AgentLoopProvider:
    """将 Service 句柄映射为一个 AgentLoop 数据面实例。"""

    def __init__(self, services: AgentRuntimeServices) -> None:
        self.services = services

    def build(self) -> AgentLoopRuntime:
        """构造 Loop，并只通过 Driver 交给 AgentService。"""
        kwargs = {
            "bus": self.services.message_bus.bus,
            "session_manager": self.services.sessions,
            "channel_manager": self.services.channels.manager,
            "event_hub": self.services.event_hub,
            "tool_registry": self.services.tools.registry,
            "command_service": self.services.commands,
            "plugin_manager": self.services.plugin_manager,
            "agent_manager": self.services.agent_profiles.manager,
            "agent_registry": (
                self.services.agents.registry if self.services.agents is not None else None
            ),
        }
        if self.services.system_prompt is not None:
            kwargs["system_prompt"] = self.services.system_prompt
        if self.services.hook_runtime is not None:
            kwargs["hook_runtime"] = self.services.hook_runtime
        if self.services.compaction is not None:
            kwargs["compaction"] = self.services.compaction
        loop = AgentLoop(**kwargs)
        bind_events = getattr(self.services.compaction, "bind_event_emitter", None)
        if callable(bind_events):
            bind_events(loop.emit_session_event)
        return AgentLoopRuntime(loop=loop, driver=AgentLoopDriver(loop))


__all__ = ["AgentLoopProvider", "AgentLoopRuntime", "AgentRuntimeServices"]
