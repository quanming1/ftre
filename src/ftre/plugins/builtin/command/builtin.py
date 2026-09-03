"""内置 Command 注册。

Handler 只依赖公开 Service，不捕获数据面运行时。需要恢复 Agent 的确认命令通过
AgentService 复用已有 Session Event 管线，不把 Agent 控制对象塞进 CommandResult。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from ftre_agent import AgentService
from ftre_agent.event import UserConfirmResultEvent
from ftre_agent.message import ToolCallBlock, ToolCallState

from ftre.services.session import SessionService
from ftre.services.session.message.converter import _as_msg

from .manager import CommandRuntime
from .types import CommandContext, CommandResult

logger = logging.getLogger(__name__)


def register_builtin_commands(
    runtime: CommandRuntime,
    *,
    agents: AgentService | Callable[[], AgentService | None],
    sessions: SessionService,
) -> list[Callable[[], bool]]:
    """注册内置命令并返回所有 disposer。"""

    def current_agents() -> AgentService:
        value = agents() if callable(agents) else agents
        if value is None:
            raise RuntimeError("AgentService runtime is not ready")
        return value

    async def on_cancel(ctx: CommandContext) -> CommandResult:
        await current_agents().cancel(ctx.session_id)
        return CommandResult.success()

    async def confirm(ctx: CommandContext, *, approved: bool) -> CommandResult:
        tool_call_ids = list(dict.fromkeys((ctx.args or "").split()))
        if not tool_call_ids:
            return CommandResult.error(
                f"用法：{ctx.command} <tool_id> [tool_id...]"
            )

        records = await sessions.get_messages_by_session(ctx.session_id)
        targets: dict[str, tuple[str, ToolCallBlock]] = {}
        wanted = set(tool_call_ids)
        for record in records:
            message = _as_msg(record)
            for block in message.content:
                if isinstance(block, ToolCallBlock) and block.id in wanted:
                    targets[block.id] = (message.id, block)

        missing = [tool_id for tool_id in tool_call_ids if tool_id not in targets]
        if missing:
            return CommandResult.error(
                f"找不到待确认的工具调用：{', '.join(missing)}"
            )

        not_asking = [
            tool_id
            for tool_id in tool_call_ids
            if targets[tool_id][1].state != ToolCallState.ASKING
        ]
        if not_asking:
            return CommandResult.error(
                f"工具调用已不在待确认状态：{', '.join(not_asking)}"
            )

        reply_ids = {targets[tool_id][0] for tool_id in tool_call_ids}
        if len(reply_ids) != 1:
            return CommandResult.error("一次确认只能处理同一条回复中的工具调用")

        reply_id = next(iter(reply_ids))
        events = [
            UserConfirmResultEvent(
                reply_id=reply_id,
                tool_call_id=tool_id,
                approved=approved,
            )
            for tool_id in tool_call_ids
        ]
        try:
            await current_agents().resume_confirmation(
                ctx.session_id,
                ctx.channel_id,
                events,
                ctx.inbound.metadata,
            )
        except Exception as exc:
            logger.exception("[command] confirmation resume failed session=%s", ctx.session_id)
            return CommandResult.error(f"确认处理失败：{exc}")
        return CommandResult.success()

    async def on_allow(ctx: CommandContext) -> CommandResult:
        return await confirm(ctx, approved=True)

    async def on_deny(ctx: CommandContext) -> CommandResult:
        return await confirm(ctx, approved=False)

    async def on_fork(ctx: CommandContext) -> CommandResult:
        if not ctx.session_id:
            return CommandResult.error("无法确定当前会话")
        try:
            result = await sessions.fork_session(ctx.session_id)
        except ValueError as exc:
            return CommandResult.error(f"fork 失败：{exc}")
        except Exception:
            logger.exception("[command] /fork failed session=%s", ctx.session_id)
            return CommandResult.error("fork 失败，请查看日志")
        return CommandResult.success(
            f"已 fork 当前会话到新会话「{result.title}」：{result.fork_session_id}"
        )

    disposers = [
        runtime.register(
            "/cancel",
            on_cancel,
            description="取消当前会话执行",
            system=True,
            persist_input=False,
        ),
        runtime.register(
            "/allow",
            on_allow,
            description="允许一个或多个待确认工具调用",
            args_hint="<tool_id> [tool_id...]",
            persist_input=False,
        ),
        runtime.register(
            "/deny",
            on_deny,
            description="拒绝一个或多个待确认工具调用",
            args_hint="<tool_id> [tool_id...]",
            persist_input=False,
        ),
        runtime.register(
            "/fork",
            on_fork,
            description="复制当前会话为一个独立的新会话",
            persist_input=False,
        ),
    ]
    return disposers


__all__ = ["register_builtin_commands"]
