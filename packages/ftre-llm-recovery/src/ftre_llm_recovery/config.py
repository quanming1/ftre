"""把用户配置转换为 LLM Recovery Plugin 使用的只读策略快照。

配置解析和策略判断刻意分开：本模块只负责清洗数据，不知道 Hook、Agent 或 LLM；
``policy.py`` 再使用这里生成的不可变对象做纯函数判断。这样配置在 Plugin 启动时只
解析一次，运行中的每次错误处理不会重新读取磁盘，也不会受到配置对象被外部修改的影响。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class RetryRule:
    """某一种错误码对应的恢复建议。

    ``action`` 只允许 ``retry`` 或 ``stop``。``delay`` 是建议 Core 在下一次尝试前
    等待的秒数；它不是超时时间。这里没有 ``max_retries``，因为最大尝试次数属于
    Core 状态机，Plugin 无权突破那个硬上限。
    """

    action: str
    delay: float | None = None


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    """一个 Plugin Fiber 生命周期内共享的只读配置快照。

    ``rules`` 按已经归一化的错误码索引规则；``exclude_codes`` 是明确交还其他
    Plugin/Core 处理的错误集合。无法识别的配置不会让 Plugin 启动失败，而是被忽略，
    最终表现为“不覆盖 Core 默认策略”。
    """

    rules: Mapping[str, RetryRule]
    exclude_codes: frozenset[str]


def parse_config(raw: Mapping[str, Any] | None) -> RecoveryConfig:
    """解析 Manifest 传入的局部配置，返回与原对象完全解耦的快照。

    这里采用宽容解析：单条规则格式错误时只忽略该规则，避免一个拼写错误导致整个
    Gateway 无法启动。策略没有命中时，Hook listener 会调用 ``next_()``，继续使用
    后续监听器或 Core 默认决定。
    """

    root = raw if isinstance(raw, Mapping) else {}
    # rules 的键就是 Core 归一化后的 LLMError.code，例如 timeout/rate_limit。
    raw_rules = root.get("rules", {})
    rules: dict[str, RetryRule] = {}
    if isinstance(raw_rules, Mapping):
        for code, value in raw_rules.items():
            if not isinstance(code, str) or not isinstance(value, Mapping):
                continue
            # 非法 action 不做猜测；忽略后由 Core 的默认错误策略兜底。
            action = value.get("action")
            if action not in {"retry", "stop"}:
                continue
            delay = _parse_delay(value.get("delay"))
            rules[code] = RetryRule(action=action, delay=delay)

    # 错误码统一为小写，避免 Provider 的大小写差异让同一规则时灵时不灵。
    raw_excludes = root.get("exclude_codes", ())
    excludes = frozenset(
        item.strip().lower()
        for item in raw_excludes
        if isinstance(item, str) and item.strip()
    ) if isinstance(raw_excludes, (list, tuple, set, frozenset)) else frozenset()
    return RecoveryConfig(
        # MappingProxyType 阻止 listener 运行期间被意外修改规则。
        rules=MappingProxyType(rules),
        exclude_codes=excludes,
    )


def _parse_delay(value: Any) -> float | None:
    """把 delay 归一化为非负秒数；无法转换时表示“不覆盖默认等待”。"""

    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


__all__ = ["RecoveryConfig", "RetryRule", "parse_config"]
