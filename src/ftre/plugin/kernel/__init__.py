"""
ftre 插件内核（kernel）— Cordis 风格插件运行时。

子模块：
- events     事件总线（5+1 分发模式）
- context    FtreContext 服务容器
- registry   插件注册表 + Plugin 基类
- lifecycle  插件实例生命周期状态机 + effect 清理
- loader     配置树驱动加载
- services   现有能力适配为 service
"""

from .context import Cleanup, FtreContext, ServiceAccessError
from .events import (
    AGENT_BEFORE_MESSAGES_BUILD,
    AGENT_BEFORE_RUN,
    INTERNAL_PLUGIN_STATUS,
    AgentRunContext,
    Disposer,
    EventHub,
    Listener,
    MessagesBuildContext,
    append_to_first_system,
    is_bailed,
)
from .lifecycle import PluginInstance, PluginState
from .loader import Entry, EntryGroup, EntryTree, PluginLoader
from .registry import Plugin, PluginDependencyCycleError, PluginRegistry
from .services import BaseService, install_core_services

# Compatibility aliases for hook constants only; the old manager/API classes are gone.
BEFORE_MESSAGES_BUILD = AGENT_BEFORE_MESSAGES_BUILD
BEFORE_AGENT_RUN = AGENT_BEFORE_RUN

__all__ = [
    "AGENT_BEFORE_MESSAGES_BUILD",
    "AGENT_BEFORE_RUN",
    "BEFORE_AGENT_RUN",
    "BEFORE_MESSAGES_BUILD",
    "INTERNAL_PLUGIN_STATUS",
    "AgentRunContext",
    "BaseService",
    "Cleanup",
    "Disposer",
    "Entry",
    "EntryGroup",
    "EntryTree",
    "EventHub",
    "FtreContext",
    "Listener",
    "MessagesBuildContext",
    "Plugin",
    "PluginDependencyCycleError",
    "PluginInstance",
    "PluginLoader",
    "PluginRegistry",
    "PluginState",
    "ServiceAccessError",
    "append_to_first_system",
    "install_core_services",
    "is_bailed",
]
