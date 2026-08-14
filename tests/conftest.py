"""测试共享配置。

把外部插件目录加入 sys.path，使测试能以顶层模块名导入外部插件
（如 octo_plugin 的 ``from _api import ...``）。这恢复了旧 PluginManager
扫描插件包时的 ``sys.path.insert`` 行为。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PLUGINS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"


def _register_external_plugins() -> None:
    """将 ``~/.ftre/plugins/`` 下每个插件包目录加入 sys.path（模块级导入）。"""
    if not _PLUGINS_DIR.is_dir():
        return
    if str(_PLUGINS_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGINS_DIR))
    for plugin_dir in sorted(_PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name.startswith(("_", ".")):
            continue
        if not (plugin_dir / "__init__.py").is_file():
            continue
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))


_register_external_plugins()
