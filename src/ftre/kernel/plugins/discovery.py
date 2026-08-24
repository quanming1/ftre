"""安全地发现 Plugin 候选，并把代码导入延迟到启用之后。

Discovery 处理的是“候选从哪里来”：仓库内置 Manifest、已安装发行物的
``ftre.plugins`` entry point，以及用户配置中的外部入口。它在 catalog 阶段只
读取字符串和发行物元数据，不 import 外部模块；只有 Loader 选中某个 Manifest
后才调用 ``resolve``。

延迟导入很重要：禁用的 Plugin 不应执行 import-time 副作用，也不应因为一个未启用
的可选依赖缺失而阻止 Gateway 启动。Discovery 只负责候选收集和入口解析，不负责
选择必选性、创建 Fiber 或执行 Plugin。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Any

from .catalog import PluginCatalog
from .manifest import PluginManifest


class PluginDiscovery:
    """收集候选并在真正启用后解析入口。

    ``plugins_dir`` 是外部本地插件的可选搜索根。默认位置为用户目录下的
    ``.ftre/plugins``；解析时会检查模块顶层路径，避免入口通过路径技巧逃逸到
    配置目录之外。
    """

    def __init__(self, *, plugins_dir: Path | None = None) -> None:
        # Discovery 不在构造阶段访问模块，只确定外部本地 Plugin 的搜索位置。
        default_root = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"
        self.plugins_dir = Path(plugins_dir) if plugins_dir else default_root

    def catalog(self, builtins: list[PluginManifest], config: dict[str, Any] | None = None) -> PluginCatalog:
        """合并内置、已安装发行物和配置候选，但不导入模块。

        ``ftre.plugins`` 是发行物的发现边界：读取 entry point 只拿元数据，
        真正的模块导入仍延迟到用户显式启用之后。内置同名 Manifest 优先，
        这样仓库内的默认 Composition 与已安装 wheel 不会产生第二个 Plugin id。

        ``config.plugins`` 只影响外部候选的登记和局部配置。``disabled``/``enabled``
        为 false 的条目可以保留在配置文件中而不解析入口，方便用户暂时禁用旧插件；
        内置候选已经存在于 catalog 时，配置只是覆盖选择/配置，不会创建第二份候选。
        """
        # 先放入 Composition 声明的内置候选，保证它们优先于同 id 的已安装包。
        catalog = PluginCatalog(builtins)
        self._add_installed_entry_points(catalog)
        raw = (config or {}).get("plugins", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise TypeError("config.plugins must be a list")
        # 只在本次配置文件内检查重复项；重复项不能靠“后一个覆盖前一个”掩盖。
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("each config.plugins entry must be an object")
            plugin_id = str(item.get("id") or item.get("name") or "").strip()
            if not plugin_id:
                raise ValueError("external plugin entry requires id/name")
            if plugin_id in seen:
                raise ValueError(f"duplicate plugin config entry: {plugin_id}")
            seen.add(plugin_id)
            # 禁用的外部条目仍然是合法配置，但不要求 entry，也不解析模块。这样
            # 用户迁移到新的 ``module:attribute`` 契约期间，可以安全保留旧声明。
            if bool(item.get("disabled", False)) or item.get("enabled") is False:
                continue
            if catalog.get(plugin_id) is not None:
                # 已存在的内置候选只接受配置覆盖，不新增第二个候选。
                continue
            entry = item.get("entry")
            if not entry:
                raise ValueError(f"external plugin {plugin_id!r} requires entry")
            source = f"external:{plugin_id}"
            catalog.add(
                PluginManifest(
                    id=plugin_id,
                    entry=str(entry),
                    source=source,
                    required=bool(item.get("required", False)),
                    default_enabled=False,
                    version=item.get("version"),
                    description=str(item.get("description", "")),
                    config=dict(item.get("config") or {}),
                )
            )
        return catalog

    @staticmethod
    def _add_installed_entry_points(catalog: PluginCatalog) -> None:
        """把已安装发行物登记为可选候选，不在发现阶段 import 代码。

        ``importlib.metadata.entry_points`` 返回的是发行物元数据；读取它不会执行
        entry point 指向的模块。entry point 名称成为外部 Plugin id，实际入口值
        留给 ``resolve`` 在 Plugin 被选择后处理。
        """
        entries = importlib.metadata.entry_points()
        # Python 3.12 返回 SelectableGroups；旧版本或测试替身可能直接返回列表。
        selected = entries.select(group="ftre.plugins") if hasattr(entries, "select") else entries
        for entry in selected:
            plugin_id = str(entry.name).strip()
            if not plugin_id or catalog.get(plugin_id) is not None:
                continue
            distribution = getattr(entry, "dist", None)
            metadata = getattr(distribution, "metadata", {}) if distribution else {}
            catalog.add(
                PluginManifest(
                    id=plugin_id,
                    entry=str(entry.value),
                    source=f"external:{plugin_id}",
                    required=False,
                    default_enabled=False,
                    version=getattr(distribution, "version", None),
                    description=str(metadata.get("Summary", "")),
                )
            )

    def resolve(self, manifest: PluginManifest) -> Any:
        """解析一个已经选中的入口；该方法只能在 enable 之后调用。

        解析阶段才可能 import 模块和修改本地插件搜索路径，因此它必须与 catalog
        分开。禁用 Plugin 不会执行 import-time 代码，也不会因为外部包存在问题而
        影响基础 Composition。
        """
        # 入口可以是测试/内置代码直接提供的可调用对象；这种情况不需要 import。
        entry = manifest.entry
        if not isinstance(entry, str):
            return entry
        module_name, separator, attribute = entry.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"plugin entry must use 'module:attribute': {entry!r}")
        if self.plugins_dir is not None:
            # 只允许本地插件从明确的 plugins root 加载。这里检查的是模块的顶层
            # 目录，防止入口字符串把 sys.path 带到用户指定目录之外。
            candidate = (self.plugins_dir / module_name.split(".")[0]).resolve()
            root = self.plugins_dir.resolve()
            if candidate.exists() and root not in candidate.parents and candidate != root:
                raise ValueError(f"plugin entry escapes plugins root: {entry!r}")
            if root.exists() and str(root) not in sys.path:
                sys.path.insert(0, str(root))
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        module = importlib.import_module(module_name)
        try:
            target = getattr(module, attribute)
            # Python 模块经常把 inject/provide 声明放在入口函数旁边，而不是函数
            # 对象属性上。复制声明后，Cordis 能让函数入口和类入口使用同一依赖
            # 契约；这里只复制元数据，不包装或执行 target。
            for key in ("inject", "provide"):
                if hasattr(module, key) and not hasattr(target, key):
                    setattr(target, key, getattr(module, key))
            return target
        except AttributeError as exc:
            raise LookupError(f"plugin attribute not found: {entry!r}") from exc
