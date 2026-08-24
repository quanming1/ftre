"""ftre Plugin Runtime 的公共导出面。

``kernel.plugins`` 是“插件从候选描述走到可运行 Fiber”的机制层。它把流程拆成
几个有明确边界的对象：

* ``PluginManifest``：描述一个候选 Plugin 的身份、入口和配置；
* ``PluginDiscovery``：收集内置、entry point 和显式配置中的候选，不提前导入代码；
* ``PluginCatalog``：检查 id 冲突并提供稳定快照；
* ``PluginLoader``：把已选候选交给官方 Cordis Fiber 执行和清理；
* ``PluginManager``：面向 Composition 的选择/装配门面；
* ``PluginStatus``：把 Fiber 状态转换成 HTTP/CLI 可用的诊断模型。

这里不定义任何产品 Plugin，也不把业务对象放进 Kernel。它只负责“发现、选择、
加载、卸载、重启和报告状态”的通用生命周期机制。
"""

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
