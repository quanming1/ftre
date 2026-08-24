"""ftre 的通用 Hook 运行时入口。

这里是业务 Plugin 使用 Hook 机制时的唯一公共导入面。可以把本目录理解成一
个很薄的适配层：

``HookSpec`` / ``HookMode`` / ``HookScope``
    来自 ``ftre-agent-core``，描述一个 Hook 的名字、调用方式和作用域。
``HookRuntime``
    负责把监听器注册到 Cordis Context，并在触发时选择 Cordis 的事件调度模式。
``HookScopeCarrier``
    给 Agent 运行实例创建隔离身份，防止不同 Agent 的监听器互相收到事件。
``HookReceipt`` / 诊断结构
    让 Plugin 能主动取消注册，也让生命周期和运维代码能观察注册及失败情况。

Kernel 只提供上述“怎么注册和调度”的机制，不知道 ``agent/run-error``、
``inbox/before-claim``、``tools/result`` 等业务 Hook 的含义。具体 HookSpec 必须由
对应的 Service 或 Package 定义，这样卸载一个可选 Plugin 时，Kernel 不需要认识
或保留它的业务协议。
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
