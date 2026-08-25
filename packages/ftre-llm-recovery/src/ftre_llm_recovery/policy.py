"""LLM 失败后的纯策略判断。

调用时机是“一次 LLM attempt 已经失败并被 Core 归一化成 LLMError 之后”。本模块只
回答是否覆盖 Core 默认决定，不执行 sleep、不重新调用模型，也不修改 attempt 计数。
因此它可以脱离 Cordis/Agent 单独测试。
"""

from __future__ import annotations

from ftre.services.llm.hooks import LLMErrorDecision, LLMErrorPayload

from .config import RecoveryConfig


def decide(payload: LLMErrorPayload, config: RecoveryConfig) -> LLMErrorDecision | None:
    """返回配置命中的 ``retry``/``stop``，返回 ``None`` 表示不干预。

    ``None`` 很重要：它不是“停止”，而是让 Plugin listener 继续调用 ``next_()``。
    Hook 链后面可能还有其他策略 Plugin；全部不处理时才使用 Core 默认值。
    """

    # Core 已经尽力归一化错误码，这里仍做一次字符串清洗，保证配置查找稳定。
    code = (payload.error_code or "").strip().lower()
    error_text = f"{code} {payload.error_message or ''}".lower()
    # 上下文溢出可能被 Provider 统称为 bad_request；不能只按粗粒度错误码
    # 截断压缩插件的恢复路径，因此内置识别常见 overflow 文本。
    overflow_markers = ("overflow", "context_length", "context length", "too_long", "too long")
    if (
        not code
        or code in config.exclude_codes
        or any(marker in error_text for marker in overflow_markers)
    ):
        return None
    # 未配置的错误绝不能擅自改成 retry，否则会改变 Core 原有的安全默认值。
    rule = config.rules.get(code)
    if rule is None:
        return None
    return LLMErrorDecision(action=rule.action, delay=rule.delay)


__all__ = ["decide"]
