"""Hook Runtime 的可审计诊断数据。

诊断对象只描述“哪个监听器、以什么顺序、在什么作用域失败”，不携带 Hook
payload。payload 往往包含用户消息、Prompt 或工具结果，把它放进诊断会造成
隐私泄露和日志膨胀；因此这里故意只保留元数据。

这些类是不可变快照，不是 Runtime 内部注册表本身。调用方拿到快照后可以安全地
展示、记录或测试，而不会改变正在运行的监听器。
"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import HookMode


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    """一次监听器异常的脱敏记录。

    ``HookRuntime`` 根据 HookSpec 的失败策略决定异常是继续向上传播还是被观察
    后吞掉；无论采取哪种策略，都会先生成这条记录。``active_calls`` 是记录生成
    时的并发调用数，可帮助判断卸载/关闭时是否存在正在执行的异步监听器。
    """

    hook: str  # 稳定的 Hook 名，例如 ``agent/request``。
    owner: str  # 注册监听器的 Plugin 或 Service Owner。
    mode: HookMode  # 该 Hook 的调度模式。
    scope: str  # 面向人类的作用域标签，不参与真正的身份匹配。
    listener_order: int  # 当前监听器在该 Hook 中的注册顺序。
    active_calls: int  # 异常发生时仍在执行的调用数量。
    exception_type: str  # 异常类名；不保存异常对象，避免持有运行时资源。
    message: str  # 脱敏后的固定描述，不包含 payload 内容。


@dataclass(frozen=True, slots=True)
class HookListenerSnapshot:
    """某个 Hook 当前注册状态的只读快照。

    ``snapshot()`` 使用它返回监听器清单，主要用于启动诊断、Plugin unload
    测试和排查“为什么这个 Hook 被调用了”。它不会暴露 listener callable，避免
    外部绕过 Runtime 直接调用或持有 Plugin 的私有对象。
    """

    hook: str  # HookSpec.name。
    owner: str  # 监听器所属 Owner。
    mode: HookMode  # HookSpec.mode。
    scope: str  # 注册时的诊断标签。
    listener_order: int  # 当前监听器顺序；prepend 会让它变成 0。
    once: bool  # 是否只允许 Cordis 调用一次。
    active_calls: int  # 当前 in-flight 调用数。
    disposed: bool  # 是否已经请求取消注册。


__all__ = ["HookDiagnostic", "HookListenerSnapshot"]
