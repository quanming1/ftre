"""AgentLoop Provider：唯一负责把公共 Service 组合成数据面 Loop。"""

from __future__ import annotations

from dataclasses import dataclass

from cordis import Context

from ftre.platform.hooks import HookRuntime
from ftre.platform.plugin_runtime.manager import PluginManager
from ftre.services.agent import AgentService
from ftre.services.agent.profile import AgentProfileService
from ftre.services.agent_loop.runtime.loop.engine import AgentLoop
from ftre.services.attachment import AttachmentService
from ftre.services.command import CommandService
from ftre.services.compaction import CompactionPort
from ftre.services.messaging.bus import MessageBusService
from ftre.services.messaging.channel import ChannelService
from ftre.services.session import SessionService
from ftre.services.system_prompt import SystemPromptService
from ftre.services.tools import ToolService

from .driver import AgentLoopDriver


@dataclass(frozen=True)
class AgentRuntimeServices:
    """构造 AgentLoop 所需的公开 Service 句柄。"""

    sessions: SessionService
    message_bus: MessageBusService
    channels: ChannelService
    tools: ToolService
    commands: CommandService
    agent_profiles: AgentProfileService
    event_hub: Context
    plugin_manager: PluginManager
    agents: AgentService | None = None
    attachments: AttachmentService | None = None
    system_prompt: SystemPromptService | None = None
    hook_runtime: HookRuntime | None = None
    compaction: CompactionPort | None = None


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
            "agent_service": self.services.agents,
            "attachments": self.services.attachments,
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
        if self.services.compaction is not None:
            self.services.compaction.bind_event_emitter(loop.emit_session_event)
        return AgentLoopRuntime(loop=loop, driver=AgentLoopDriver(loop))


__all__ = ["AgentLoopProvider", "AgentLoopRuntime", "AgentRuntimeServices"]
