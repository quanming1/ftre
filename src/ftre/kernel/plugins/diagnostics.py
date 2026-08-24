"""Plugin 生命周期诊断模型。

Loader 内部使用 Cordis ``Fiber``，但 HTTP、CLI、启动日志和测试不应该依赖 Fiber
的内部对象。这里把状态转换成稳定、可序列化的 ``PluginStatus``，同时保留失败
原因、缺失依赖、启动耗时和需要重启的提示。

诊断只描述结果，不负责修复失败、重试或启动 Plugin；那些动作分别属于 Loader
和 Manager。``PluginStartupError`` 用来把“必选 Plugin 没有 ACTIVE”作为一个
明确的启动失败向上报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cordis import FiberState


@dataclass(frozen=True)
class PluginStatus:
    """一个 Plugin Fiber 的稳定、可序列化状态视图。

    ``state`` 保留 Cordis 的 ``FiberState``，但也接受字符串，方便测试替身或
    外部状态适配。``contributions`` 是产品层记录该 Plugin 注册了哪些 Route、
    Hook 或 Tool 的摘要；Kernel 只透传它，不解释具体贡献内容。
    """

    id: str  # 稳定 Plugin id，来自 Manifest。
    source: str  # builtin 或 external:*，帮助定位 Owner/发行物来源。
    entry: str  # module:attribute 或可调用对象的诊断文本。
    state: FiberState | str  # Cordis Fiber 当前状态。
    required: bool  # 必选 Plugin 失败时会阻止 Gateway 启动。
    error: str | None = None  # 脱敏后的异常文本；成功时为空。
    error_code: str | None = None  # 供客户端稳定判断错误类别。
    missing: tuple[str, ...] = ()  # 当前 Fiber 声明但尚未提供的 Service key。
    restart_required: bool = False  # Host 表面变化后是否需要重启 Gateway。
    duration_ms: float | None = None  # 从 Loader 开始处理到生成状态的耗时。
    contributions: tuple[dict[str, Any], ...] = ()  # 对外能力注册摘要。

    def as_dict(self) -> dict[str, Any]:
        """把状态转换为 HTTP/CLI 可以直接使用的 JSON 结构。

        Enum 转成字符串、tuple 转成 list、贡献项做浅复制；不会把 Fiber、异常
        对象或内部 Context 泄露给外部调用方。
        """
        return {
            "id": self.id,
            "source": self.source,
            "entry": self.entry,
            "state": self.state.value if isinstance(self.state, FiberState) else self.state,
            "required": self.required,
            "error": self.error,
            "error_code": self.error_code,
            "missing": list(self.missing),
            "restart_required": self.restart_required,
            "duration_ms": self.duration_ms,
            "contributions": [dict(item) for item in self.contributions],
        }


class PluginStartupError(RuntimeError):
    """一个或多个 required Plugin 无法进入 ``ACTIVE`` 时抛出的启动异常。

    ``statuses`` 保留完整状态快照，让启动命令可以同时报告“哪个 Plugin 失败、
    缺少什么依赖、入口是否导入成功”，而不是只显示一条没有上下文的字符串。
    """

    def __init__(self, message: str, statuses: tuple[PluginStatus, ...] = ()) -> None:
        super().__init__(message)
        self.statuses = statuses
