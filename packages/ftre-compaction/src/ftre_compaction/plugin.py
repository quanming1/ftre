"""ftre-compaction 的唯一装配入口。

这个模块只做三件事：拿到 ftre 提供的公开 Service、创建本包的
``CompactionService``、把 Hook 和 Command 的注册绑定到 Cordis effect。
它不实现压缩算法，也不在核心 Gateway 中添加任何条件分支。这样卸载本
插件时，Hook、命令和正在运行的压缩任务都能沿同一个生命周期反向清理。
"""

from __future__ import annotations

from cordis import Context

from .commands import register_commands
from .config import CompactionConfig
from .hooks import register_hooks
from .service import CompactionService

# config 是公开 ConfigService，不是 AgentConfig。压缩包通过 snapshot() 读取
# 自己拥有的配置字段；sessions/hook_runtime/commands 则分别对应持久化、Hook
# 调度和 Command Plane 的稳定契约。
inject = ("config", "sessions", "session_events", "hook_runtime", "commands", "inbox", "llm")
provide = ("compaction",)


def apply(ctx: Context, config=None):
    """发布压缩 Service，并可逆地注册 Hook 与命令。

    ``config`` 是 Plugin Manifest 的局部覆盖（例如测试或单独部署时设置
    ``threshold``）；``ctx.config`` 才是运行期配置的 Owner。两者不混用：
    局部覆盖只作为默认值，Hook 每次执行会从 ConfigService 创建不可变快照。
    """
    service = ctx.get("compaction", strict=False)
    if service is None:
        options = config if isinstance(config, dict) else {}
        service = CompactionService(
            session_manager=ctx.sessions,
            llm=ctx.llm,
            emit_event=ctx.session_events.emit,
            threshold=float(options.get("threshold", 0.8)),
            config_service=ctx.config,
            default_config=CompactionConfig(
                compact_threshold=float(options.get("threshold", 0.8)),
            ),
        )
        ctx.provide("compaction", service)
        ctx.effect(lambda: service.close, label="ftre-compaction:close")

    # HookRuntime 已把每个 receipt 绑定到当前 Plugin Fiber；Plugin 不重复注册 disposer。
    register_hooks(ctx, service)
    for index, disposer in enumerate(register_commands(ctx.commands, service)):
        ctx.effect(
            lambda disposer=disposer: disposer,
            label=f"ftre-compaction:command:{index}",
        )


__all__ = ["apply", "inject", "provide"]
