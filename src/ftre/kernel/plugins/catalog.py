"""Plugin Manifest 候选目录。

目录把“有哪些候选 Plugin”与“哪些候选现在要启动”分开。它只保存已经解析出的
Manifest 元数据，负责检查稳定 id 是否冲突；模块导入、依赖判断和 Fiber 启动由
Discovery/Loader 负责。这样配置错误能在启动前尽早暴露，也不会因为后加入的候选
悄悄覆盖前一个同名 Plugin。
"""

from __future__ import annotations

from collections.abc import Iterable

from .manifest import PluginManifest


class PluginCatalog:
    """按稳定 Plugin id 索引候选 Manifest 的目录。

    构造和 ``add`` 阶段允许 Composition/Discovery 逐步收集候选；对外的
    ``values``/``snapshot`` 返回 tuple，避免消费者直接修改内部字典。目录本身
    不负责 import、enable、启动或卸载，所以它不是 Plugin Manager 的替代品。
    """

    def __init__(self, manifests: Iterable[PluginManifest] = ()) -> None:
        # dict 保持插入顺序：状态输出和测试需要稳定顺序，但 id 仍然是唯一键。
        self._items: dict[str, PluginManifest] = {}
        for manifest in manifests:
            self.add(manifest)

    def add(self, manifest: PluginManifest) -> None:
        """加入一个候选；重复 id 是配置错误，不是覆盖关系。

        内置 Manifest 和已安装发行物同名时，Discovery 会在调用本方法前决定
        优先级；Catalog 本身不猜测哪个 Owner 更正确。
        """
        previous = self._items.get(manifest.id)
        if previous is not None:
            raise ValueError(
                f"plugin id conflict: {manifest.id!r} ({previous.source} vs {manifest.source})"
            )
        self._items[manifest.id] = manifest

    def get(self, plugin_id: str) -> PluginManifest | None:
        """按稳定 id 查找候选；不存在时返回 ``None``。"""
        return self._items.get(plugin_id)

    def require(self, plugin_id: str) -> PluginManifest:
        """按稳定 id 查找候选；不存在时抛出明确的 ``KeyError``。"""
        manifest = self.get(plugin_id)
        if manifest is None:
            raise KeyError(plugin_id)
        return manifest

    def values(self) -> tuple[PluginManifest, ...]:
        """以稳定插入顺序返回候选快照。"""
        return tuple(self._items.values())

    def snapshot(self) -> tuple[PluginManifest, ...]:
        """返回不可变目录快照，供诊断和测试使用。

        ``values`` 与 ``snapshot`` 当前都返回 tuple；两个名字分别表达“遍历目录”
        和“取得审计快照”的调用意图，均不会暴露内部 dict。
        """
        return tuple(self._items.values())

    def __contains__(self, plugin_id: str) -> bool:
        """支持 ``plugin_id in catalog`` 的存在性检查。"""
        return plugin_id in self._items
