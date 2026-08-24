"""Cordis Plugin 入口周围的稳定 ftre 元数据。

Manifest 是 Composition、Discovery 和 Loader 之间的边界对象。它只描述“这个
Plugin 是谁、入口在哪里、是否必选、启动时拿到什么配置”，不保存 Fiber，也不
提前执行入口代码。这样可以在真正 import/启动之前完成 id、来源和入口形状校验。

注意：``entry`` 允许字符串 ``module:attribute``、函数、类或带 ``apply`` 的对象，
这是 Loader 对内置和外部 Plugin 的统一输入；最终的调用形状由 ``PluginLoader``
适配到官方 Cordis Fiber。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class PluginManifest:
    """经过校验的 Plugin 身份、入口和配置元数据。

    ``required`` 表示启动策略，不表示该 Plugin 一定会被选中；是否启用还要由
    Composition 默认值和用户配置共同决定。``default_enabled`` 表示候选的默认
    选择倾向，真正的选择由 ``PluginManager`` 完成。
    """

    id: str  # 稳定、唯一、用于配置和诊断的 Plugin id。
    entry: str | Callable[..., Any] | Any  # 入口字符串或可调用/带 apply 的对象。
    source: str = "builtin"  # builtin 或 external[:distribution/plugin_id]。
    required: bool = False  # 失败是否阻止整个 Gateway 启动。
    default_enabled: bool = True  # Composition 未覆盖时是否默认选择。
    version: str | None = None  # 外部发行物版本，可为空。
    description: str = ""  # 面向诊断/管理界面的说明。
    config: dict[str, Any] = field(default_factory=dict)  # 传给该 Plugin 的局部配置。

    def __post_init__(self) -> None:
        # id 是配置、状态接口和 entry point 的稳定连接点，必须在 Manifest
        # 创建时拒绝大小写/符号不一致的值，而不是等到启动后才发现冲突。
        if not PLUGIN_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid plugin id: {self.id!r}")
        if self.source not in {"builtin", "external"} and not self.source.startswith("external:"):
            raise ValueError(f"invalid plugin source: {self.source!r}")
        if not callable(self.entry) and not isinstance(self.entry, str) and not hasattr(self.entry, "apply"):
            raise TypeError(f"plugin {self.id!r} entry is not callable")

    @property
    def entry_text(self) -> str:
        """把入口渲染成诊断中的统一文本。

        字符串入口原样保留；函数/类/对象用 ``module:attribute`` 形式展示。该
        属性只用于日志和状态输出，不会触发入口调用。
        """
        return self.entry if isinstance(self.entry, str) else f"{self.entry.__module__}:{self.entry.__name__}"

    def with_config(self, config: dict[str, Any] | None) -> PluginManifest:
        """返回带局部配置覆盖的新 Manifest。

        Manifest 是 frozen dataclass，配置覆盖不能原地修改原对象；这里复制原
        配置再合并 override，并保留 id、入口、来源和 required 等身份元数据。配置
        的语义属于具体 Plugin，Kernel 只负责传递，不解析其中的业务字段。
        """
        merged = dict(self.config)
        if config:
            merged.update(config)
        return PluginManifest(
            id=self.id,
            entry=self.entry,
            source=self.source,
            required=self.required,
            default_enabled=self.default_enabled,
            version=self.version,
            description=self.description,
            config=merged,
        )
