"""Project-level plugin runtime adapters."""
# 中文说明：Plugin Runtime 公共导出：Manifest、Discovery、Loader、Manager 和诊断对象的统一入口。

from .catalog import PluginCatalog
from .diagnostics import PluginStartupError, PluginStatus
from .discovery import PluginDiscovery
from .loader import PluginLoader
from .manager import PluginManager
from .manifest import PluginManifest

__all__ = [
    "PluginCatalog",
    "PluginDiscovery",
    "PluginLoader",
    "PluginManager",
    "PluginManifest",
    "PluginStartupError",
    "PluginStatus",
]
