"""解析 Fallback Plugin 自己拥有的、不可变的模型切换配置。

配置只保存“备用模型是谁”和“哪些主模型错误允许切换”。API Key、Base URL 等凭据仍由
Host 的 ConfigService 统一解析，避免 Plugin 再造第二份 Provider 配置来源。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FallbackConfig:
    """一个 Plugin Fiber 生命周期内使用的备用模型策略快照。

    ``provider``/``model`` 是 Host 配置中的逻辑名称，不是直接传给 SDK 的完整配置；
    ``errors`` 是允许接管的主模型错误码；``exclude_errors`` 拥有更高优先级，用来明确
    排除应该交给 Compaction 或 Runtime 的错误。
    """

    provider: str
    model: str
    errors: frozenset[str]
    exclude_errors: frozenset[str]

    @property
    def enabled(self) -> bool:
        """只有模型坐标和错误白名单同时存在时，Fallback 才算启用。"""

        # 不提供“匹配所有错误”的隐式默认值，避免错误配置悄悄改变生产模型行为。
        return bool(self.provider and self.model and self.errors)


def parse_config(raw: Mapping[str, Any] | None) -> FallbackConfig:
    """从 Manifest 局部配置创建只读快照，不接受任何凭据覆盖。

    如果 provider/model/errors 缺失，返回的配置会自然处于 disabled 状态；Plugin 仍可
    安全加载，但 ``llm/stream`` 会完全透传主模型流。
    """

    root = raw if isinstance(raw, Mapping) else {}
    provider = root.get("provider", "")
    model = root.get("model", "")
    return FallbackConfig(
        provider=provider.strip() if isinstance(provider, str) else "",
        model=model.strip() if isinstance(model, str) else "",
        errors=_string_set(root.get("errors", ())),
        exclude_errors=_string_set(root.get("exclude_errors", ())),
    )


def _string_set(value: Any) -> frozenset[str]:
    """把错误码列表清洗成小写去重集合，非法类型视为空集合。"""

    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(item.strip().lower() for item in value if isinstance(item, str) and item.strip())


__all__ = ["FallbackConfig", "parse_config"]
