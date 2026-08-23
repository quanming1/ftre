"""ftre HookRuntime 的通用机制适配。

Kernel 只负责把类型化 HookSpec 交给 Cordis 调度，并提供作用域、诊断、receipt
和取消能力。具体的 ``agent/*``、``session/*`` 或 ``tools/*`` 名称由语义 Owner
定义在各自 Service/Package 中，避免 Kernel 重新拥有产品协议。
"""

from .diagnostics import HookDiagnostic, HookListenerSnapshot
from .runtime import HookReceipt, HookRuntime
from .scope import HookScopeCarrier, context_for_scope
from .spec import HookFailurePolicy, HookMode, HookScope, HookSpec

__all__ = [
    "HookDiagnostic",
    "HookFailurePolicy",
    "HookListenerSnapshot",
    "HookMode",
    "HookReceipt",
    "HookRuntime",
    "HookScope",
    "HookScopeCarrier",
    "HookSpec",
    "context_for_scope",
]
