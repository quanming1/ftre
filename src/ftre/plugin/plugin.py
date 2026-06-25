"""
ftre Plugin 体系

- Plugin: 插件基类
- FtrePluginApi: 插件操作接口
- PluginManager: 插件生命周期管理

加载规则：
- 扫描 ~/.ftre/plugins/ 目录下所有 .py 文件
- 找到 Plugin 子类自动实例化并加载
- 配置文件 ~/.ftre/config.json 的 plugins 数组提供每个插件的 config
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ftre.bus import EventBus
from ftre.channel import Channel, ChannelManager
from ftre_agent_core.tool import Tool
from ftre.tools import ToolRegistry

from .hook_manager import HookManager

if TYPE_CHECKING:
    from ftre.session import SessionManager

logger = logging.getLogger(__name__)

# 插件目录固定位置
PLUGINS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"


class FtrePluginApi:
    """ftre 暴露给插件的 API"""

    def __init__(
        self,
        bus: EventBus,
        channel_manager: ChannelManager,
        session_manager: "SessionManager",
        hook_manager: HookManager,
        config: dict,
        tool_registry: ToolRegistry,
        event_loop: Callable | None = None,
        command_manager: object | None = None,
        appended_system_prompts: list[str] | None = None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.config = config
        self._hook_manager = hook_manager
        self._tool_registry = tool_registry
        self._event_loop = event_loop
        self._command_manager = command_manager
        self._appended_system_prompts = appended_system_prompts if appended_system_prompts is not None else []

    @property
    def event_loop(self):
        """主 asyncio 事件循环引用，用于 run_coroutine_threadsafe。"""
        if callable(self._event_loop):
            return self._event_loop()
        return self._event_loop

    @property
    def command_manager(self):
        """Command 管理器。"""
        return self._command_manager

    def register_channel(self, channel: Channel) -> None:
        """注册 Channel"""
        self.channel_manager.register(channel)

    @property
    def tool_registry(self) -> ToolRegistry:
        """共享工具注册表，插件可注册 / 过滤工具。"""
        return self._tool_registry

    def register_hook(self, point: str, fn: Callable) -> None:
        """
        在指定 hook point 注册一个 hook 函数。

        见 plugin/hook_manager.py 的 hook point 常量与 Context 定义。
        hook 函数签名：(ctx) -> ctx，hook 内部抛异常会被捕获不影响主流程。
        """
        self._hook_manager.register(point, fn)

    def append_system_prompt(self, text: str) -> None:
        """
        追加内容到 system prompt 末尾。

        插件可通过此方法向所有会话的 system prompt 注入额外的上下文或指令。
        内容会在每次构建 messages 时附加到 system prompt 后面。

        Args:
            text: 要追加的文本内容
        """
        if text and text.strip():
            self._appended_system_prompts.append(text)

    @property
    def appended_system_prompts(self) -> list[str]:
        """获取所有插件追加的 system prompt 内容。"""
        return self._appended_system_prompts.copy()


class Plugin:
    """插件基类。子类实现 setup()，通过 self.api 注册能力。"""
    name: str = ""
    version: str = "0.0.0"
    api: FtrePluginApi

    def setup(self) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        pass


class PluginManager:
    """插件生命周期管理"""

    def __init__(
        self,
        bus: EventBus,
        channel_manager: ChannelManager,
        session_manager: "SessionManager",
        hook_manager: HookManager,
        tool_registry: ToolRegistry | None = None,
        event_loop: Callable | None = None,
        command_manager: object | None = None,
    ):
        self._bus = bus
        self._channel_manager = channel_manager
        self._session_manager = session_manager
        self._hook_manager = hook_manager
        self._plugins: dict[str, Plugin] = {}
        self._tool_registry = tool_registry if tool_registry is not None else ToolRegistry()
        self._event_loop = event_loop
        self._command_manager = command_manager
        self._appended_system_prompts: list[str] = []

    def load_all(self, config_data: dict = None) -> None:
        """
        加载插件：先加载内置插件（ftre.plugin.builtin），再扫描 ~/.ftre/plugins/。
        config_data 中 plugins 数组按 name 匹配提供 config。
        """
        # 从配置文件构建 name → config
        configs: dict[str, dict] = {}
        if config_data:
            for entry in config_data.get("plugins", []):
                name = entry.get("name", "")
                if name:
                    configs[name] = entry.get("config", {})

        # ─── 阶段 1: 加载内置插件 ─────────────────────────────
        BUILTIN_DIR = Path(__file__).parent / "builtin"
        logger.info(f"[plugin] 加载内置插件: {BUILTIN_DIR}")
        
        for py_file in BUILTIN_DIR.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = f"ftre.plugin.builtin.{py_file.stem}"
            try:
                mod = importlib.import_module(module_name)
                for attr in vars(mod).values():
                    if (isinstance(attr, type)
                        and issubclass(attr, Plugin)
                        and attr is not Plugin
                        and attr.name):
                        plugin = attr()
                        self._load(plugin, configs.get(plugin.name, {}))
            except Exception as e:
                logger.error(f"[plugin] 内置插件 {py_file.name} 加载失败: {e}")

        # ─── 阶段 2: 扫描外部插件目录 ─────────────────────────
        logger.warning(f"[plugin] 扫描外部插件: {PLUGINS_DIR}")
        if not PLUGINS_DIR.exists():
            return

        if str(PLUGINS_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGINS_DIR))

        for py_file in PLUGINS_DIR.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(py_file.stem)
                for attr in vars(mod).values():
                    if (isinstance(attr, type)
                        and issubclass(attr, Plugin)
                        and attr is not Plugin
                        and attr.name):
                        plugin = attr()
                        self._load(plugin, configs.get(plugin.name, {}))
            except Exception as e:
                logger.error(f"[plugin] {py_file.name} 加载失败: {e}")

    def _load(self, plugin: Plugin, config: dict) -> None:
        """加载单个插件"""
        if plugin.name in self._plugins:
            return

        plugin.api = FtrePluginApi(
            bus=self._bus,
            channel_manager=self._channel_manager,
            session_manager=self._session_manager,
            hook_manager=self._hook_manager,
            config=config,
            tool_registry=self._tool_registry,
            event_loop=self._event_loop,
            command_manager=self._command_manager,
            appended_system_prompts=self._appended_system_prompts,
        )

        tool_count = len(self._tool_registry)
        try:
            plugin.setup()
            self._plugins[plugin.name] = plugin
            logger.warning(f"[plugin] 已加载: {plugin.name} v{plugin.version}")
        except Exception as e:
            self._tool_registry.truncate(tool_count)
            logger.error(f"[plugin] {plugin.name} setup 失败: {e}")

    def unload(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if plugin:
            plugin.teardown()

    def list(self) -> list[dict]:
        return [{"name": p.name, "version": p.version} for p in self._plugins.values()]

    def tools(self) -> list[Tool]:
        """返回插件注册的工具。"""
        return self._tool_registry.snapshot()

    @property
    def tool_registry(self) -> ToolRegistry:
        """插件工具注册表。"""
        return self._tool_registry

    @property
    def appended_system_prompts(self) -> list[str]:
        """获取所有插件追加的 system prompt 内容。"""
        return self._appended_system_prompts.copy()
