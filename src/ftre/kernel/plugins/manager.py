"""面向 Composition 的 Plugin 装配门面。

``PluginManager`` 不实现第二个生命周期；它只把 Composition 的内置 Manifest、
用户配置和 Discovery 目录交给 ``PluginLoader``，决定本次启动选择哪些候选，
然后提供 unload/restart/status 等面向应用层的窄入口。

因此调用关系是：

```text
Composition → PluginManager.load
             → Discovery.catalog（收集候选）
             → 配置筛选
             → PluginLoader.load
             → Cordis Context/Fiber（真正运行和清理）
```

Manager 不应该直接构造业务 Service，也不应该 import 某个具体 Plugin 的私有实现。
"""

from __future__ import annotations

from typing import Any

from cordis import Context

from .catalog import PluginCatalog
from .diagnostics import PluginStatus
from .loader import PluginLoader
from .manifest import PluginManifest


class PluginManager:
    """在 Loader 之上应用配置选择的 Composition 门面。

    这里的“门面”只表示应用层入口较窄，不表示它拥有另一份 Plugin 状态；真正的
    Fiber、错误和生命周期句柄始终由 ``self.loader`` 持有。
    """

    def __init__(self, context: Context, *, plugins_dir=None) -> None:
        # 延迟导入 Discovery 是为了保持模块边界；Manager 仍然只组合 Kernel
        # 机制对象，不直接接触外部 Plugin 模块。
        from .discovery import PluginDiscovery

        self.context = context
        self.loader = PluginLoader(context, discovery=PluginDiscovery(plugins_dir=plugins_dir))
        self.catalog: PluginCatalog | None = None
        self._statuses: tuple[PluginStatus, ...] = ()

    async def load(
        self,
        builtins: list[PluginManifest],
        config: dict[str, Any] | None = None,
    ) -> tuple[PluginStatus, ...]:
        """构建目录、应用启用配置，再加载最终选中的 Fiber。

        内置候选默认由 Composition 决定；外部候选必须在 ``config.plugins`` 中
        显式出现才会被选中。``disabled`` 或 ``enabled: false`` 会跳过候选，
        ``required`` 和 ``config`` 则作为本次启动的覆盖值传给 Loader。
        """
        self.catalog = self.loader.discovery.catalog(builtins, config)
        entries = config.get("plugins", []) if isinstance(config, dict) else []
        overrides = {
            str(item.get("id") or item.get("name")): item
            for item in entries
            if isinstance(item, dict)
        }
        selected: list[PluginManifest] = []
        for manifest in self.catalog.values():
            # 这里做的是“选择”，不是启动；入口解析和 Fiber 创建仍留在 Loader。
            override = overrides.get(manifest.id, {})
            disabled = bool(override.get("disabled", override.get("enabled") is False))
            if manifest.source.startswith("external:") and not override:
                continue
            if disabled:
                continue
            required = manifest.required or bool(override.get("required", False))
            selected.append(
                PluginManifest(
                    id=manifest.id,
                    entry=manifest.entry,
                    source=manifest.source,
                    required=required,
                    default_enabled=manifest.default_enabled,
                    version=manifest.version,
                    description=manifest.description,
                    config={**manifest.config, **dict(override.get("config") or {})},
                )
            )
        self._statuses = await self.loader.load(selected)
        return self._statuses

    def statuses(self) -> tuple[PluginStatus, ...]:
        """返回适合启动日志和健康接口的当前 Fiber 状态。"""
        return self.loader.statuses() or self._statuses

    def diagnostics(self) -> list[dict[str, Any]]:
        """返回可直接写入 JSON 响应或日志的状态字典列表。"""
        return [status.as_dict() for status in self.statuses()]

    async def unload(self, plugin_id: str) -> bool:
        """委托可逆卸载，同时保持应用层稳定入口。"""
        return await self.loader.unload(plugin_id)

    async def restart(self, plugin_id: str) -> bool:
        """通过官方 Cordis 生命周期重启一个 Plugin Fiber。"""
        return await self.loader.restart(plugin_id)

    async def close(self) -> None:
        """关闭根 Context；重复调用由 Cordis 保证安全。"""
        await self.loader.dispose()
