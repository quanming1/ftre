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
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ftre.bus import EventBus
from ftre.channel import Channel, ChannelManager

if TYPE_CHECKING:
    from ftre.session import SessionManager

logger = logging.getLogger(__name__)

# 插件目录固定位置
PLUGINS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"


class FtrePluginApi:
    """ftre 暴露给插件的 API"""

    def __init__(self, bus: EventBus, channel_manager: ChannelManager, session_manager: "SessionManager", config: dict):
        self.bus = bus
        self.session_manager = session_manager
        self.config = config
        self._channel_manager = channel_manager

    def register_channel(self, channel: Channel) -> None:
        """注册 Channel"""
        self._channel_manager.register(channel)


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

    def __init__(self, bus: EventBus, channel_manager: ChannelManager, session_manager: "SessionManager"):
        self._bus = bus
        self._channel_manager = channel_manager
        self._session_manager = session_manager
        self._plugins: dict[str, Plugin] = {}

    def load_all(self, config_data: dict = None) -> None:
        """
        扫描 ~/.ftre/plugins/ 加载所有插件。
        config_data 中 plugins 数组按 name 匹配提供 config。
        """
        # 从配置文件构建 name → config
        configs: dict[str, dict] = {}
        if config_data:
            for entry in config_data.get("plugins", []):
                name = entry.get("name", "")
                if name:
                    configs[name] = entry.get("config", {})

        # 扫描目录
        logger.warning(f"[plugin] 扫描: {PLUGINS_DIR}")
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
            config=config,
        )

        try:
            plugin.setup()
            self._plugins[plugin.name] = plugin
            logger.warning(f"[plugin] 已加载: {plugin.name} v{plugin.version}")
        except Exception as e:
            logger.error(f"[plugin] {plugin.name} setup 失败: {e}")

    def unload(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if plugin:
            plugin.teardown()

    def list(self) -> list[dict]:
        return [{"name": p.name, "version": p.version} for p in self._plugins.values()]
