"""内置指令注册。

把 loop.py 里硬编码的 3 条指令抽出来，loop.py 只需调用 register_builtin_commands()。
handler 通过闭包捕获 loop 实例，不需要往 ctx.meta 里塞 _loop。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ftre.command.types import Handled, ResumeAgent, SendMessage

if TYPE_CHECKING:
    from ftre.command.manager import CommandManager
    from ftre.agent.loop import AgentLoop

logger = logging.getLogger(__name__)


def register_builtin_commands(mgr: "CommandManager", loop: "AgentLoop") -> None:
    """注册内置斜杠指令到 CommandManager。

    :param mgr: CommandManager 实例
    :param loop: AgentLoop 实例（handler 通过闭包捕获）
    """

    # /cancel：系统级指令，在锁外执行，立即取消当前 session 的 Agent
    def _on_cancel(ctx) -> Handled:
        sid = ctx.meta.inbound.from_session or ctx.meta.inbound.data.get(
            "session_id", ""
        )
        if loop.cancel_session(sid):
            logger.info(f"[command] cancel 已取消 session={sid}")
        return Handled()

    async def _confirm_tools(ctx, *, approved: bool):
        """把 /allow、/deny 参数解析为核心确认事件。

        tool_call_id 是客户端唯一需要提交的身份；reply_id 从当前会话
        已持久化的 AssistantMsg 反查，避免让传输层维护核心事件字段。
        """
        tool_call_ids = list(dict.fromkeys((ctx.args or "").split()))
        if not tool_call_ids:
            return SendMessage(
                f"用法：{ctx.command} <tool_id> [tool_id...]",
                level="error",
            )

        from ftre.session.message.converter import _as_msg
        from ftre_agent_core.event import UserConfirmResultEvent
        from ftre_agent_core.message import ToolCallBlock, ToolCallState

        inbound = ctx.meta.inbound
        session_id = inbound.from_session or inbound.data.get("session_id", "")
        records = await loop.session_manager.get_messages_by_session(session_id)
        targets: dict[str, tuple[str, ToolCallBlock]] = {}
        wanted = set(tool_call_ids)
        for record in records:
            message = _as_msg(record)
            for block in message.content:
                if (
                    isinstance(block, ToolCallBlock)
                    and block.id in wanted
                ):
                    targets[block.id] = (message.id, block)

        missing = [tool_id for tool_id in tool_call_ids if tool_id not in targets]
        if missing:
            return SendMessage(
                f"找不到待确认的工具调用：{', '.join(missing)}",
                level="error",
            )

        not_asking = [
            tool_id
            for tool_id in tool_call_ids
            if targets[tool_id][1].state != ToolCallState.ASKING
        ]
        if not_asking:
            return SendMessage(
                f"工具调用已不在待确认状态：{', '.join(not_asking)}",
                level="warning",
            )

        reply_ids = {targets[tool_id][0] for tool_id in tool_call_ids}
        if len(reply_ids) != 1:
            return SendMessage(
                "一次确认只能处理同一条回复中的工具调用",
                level="error",
            )
        reply_id = next(iter(reply_ids))
        return ResumeAgent(events=[
            UserConfirmResultEvent(
                reply_id=reply_id,
                tool_call_id=tool_id,
                approved=approved,
            )
            for tool_id in tool_call_ids
        ])

    async def _on_allow(ctx):
        return await _confirm_tools(ctx, approved=True)

    async def _on_deny(ctx):
        return await _confirm_tools(ctx, approved=False)

    # /compact [提示词]：普通指令，在锁内执行，串行安全
    # 可选参数是自然语言提示词，透传给摘要 LLM，强调优先保留的上下文。
    async def _on_compact(ctx) -> Handled:
        inbound = ctx.meta.inbound
        session_id = inbound.from_session
        channel_id = inbound.from_channel
        focus_hint = (ctx.args or "").strip()

        loop._compacting_sessions.add(session_id)
        await loop._publish_session_status_async(session_id, "compacting")

        try:
            config = loop._load_current_config()
            await loop.compact_manager.compact(
                session_id,
                channel_id,
                config=config,
                trigger="manual",
                focus_hint=focus_hint,
            )
        except Exception:
            logger.exception(f"[command] /compact 执行异常 session={session_id}")
        finally:
            loop._compacting_sessions.discard(session_id)
            await loop._publish_session_status_async(
                session_id, loop.get_session_status(session_id)
            )
        return Handled()

    # /compress-fast [轮数]：零 LLM 成本的快速压缩
    # 可选整数参数 = 保护最近 N 轮对话内的工具输出不被裁剪（默认 0=全裁）。
    async def _on_compress_fast(ctx) -> Handled:
        inbound = ctx.meta.inbound
        session_id = inbound.from_session
        channel_id = inbound.from_channel

        arg = (ctx.args or "").strip()
        keep_turns = int(arg) if arg.isdigit() else 0

        try:
            config = loop._load_current_config()
            await loop.compact_manager.compress_fast(
                session_id,
                channel_id,
                config=config,
                keep_turns=keep_turns,
            )
        except Exception:
            logger.exception(f"[command] /compress-fast 执行异常 session={session_id}")
        return Handled()

    # /fork：把当前会话复制成一个独立的新会话（沿用 channel/workspace，复制
    # 消息与 metadata，追加 forked_from 溯源）。不入库对话本身。
    async def _on_fork(ctx) -> SendMessage:
        inbound = ctx.meta.inbound
        session_id = inbound.from_session or inbound.data.get("session_id", "")
        if not session_id:
            return SendMessage("无法确定当前会话", level="error")
        try:
            result = await loop.session_manager.fork_session(session_id)
        except ValueError as e:
            return SendMessage(f"fork 失败：{e}", level="error")
        except Exception:
            logger.exception(f"[command] /fork 执行异常 session={session_id}")
            return SendMessage("fork 失败，请查看日志", level="error")
        return SendMessage(
            f"已 fork 当前会话到新会话「{result.title}」：{result.fork_session_id}",
            level="info",
        )

    mgr.register(
        "/cancel",
        _on_cancel,
        description="取消当前会话执行",
        system=True,
        persist_input=False,
    )
    mgr.register(
        "/allow",
        _on_allow,
        description="允许一个或多个待确认工具调用",
        args_hint="<tool_id> [tool_id...]",
        persist_input=False,
    )
    mgr.register(
        "/deny",
        _on_deny,
        description="拒绝一个或多个待确认工具调用",
        args_hint="<tool_id> [tool_id...]",
        persist_input=False,
    )
    mgr.register(
        "/compact",
        _on_compact,
        description="压缩当前会话上下文（可附提示词强调优先保留的内容）",
        args_hint="[强调保留的内容]",
    )
    mgr.register(
        "/compress-fast",
        _on_compress_fast,
        description="快速压缩：裁剪旧工具输出，不调 LLM（可附轮数保护最近 N 轮）",
        args_hint="[保护最近轮数]",
    )
    mgr.register(
        "/fork",
        _on_fork,
        description="复制当前会话为一个独立的新会话",
        persist_input=False,
    )
