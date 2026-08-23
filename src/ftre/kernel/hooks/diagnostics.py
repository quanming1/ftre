"""Hook Runtime 的可审计诊断结构。"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import HookMode


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    """不包含 payload 的失败记录，避免诊断泄露用户数据。"""

    hook: str
    owner: str
    mode: HookMode
    scope: str
    listener_order: int
    active_calls: int
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class HookListenerSnapshot:
    """当前监听器快照，用于诊断顺序、owner 和 in-flight 调用。"""

    hook: str
    owner: str
    mode: HookMode
    scope: str
    listener_order: int
    once: bool
    active_calls: int
    disposed: bool


__all__ = ["HookDiagnostic", "HookListenerSnapshot"]
