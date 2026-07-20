"""内置指令注册。

把 loop.py 里硬编码的 3 条指令抽出来，loop.py 只需调用 register_builtin_commands()。
handler 通过闭包捕获 loop 实例，不需要往 ctx.meta 里塞 _loop。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ftre.command.types import Handled

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
        agent = loop._active_agents.get(sid)
        if agent:
            agent.cancel_nowait()
        task = loop._session_tasks.get(sid)
        if task and not task.done():
            task.cancel()
            logger.info(f"[command] cancel task 已取消 session={sid}")
        return Handled()

    # /compact：普通指令，在锁内执行，串行安全
    async def _on_compact(ctx) -> Handled:
        inbound = ctx.meta.inbound
        session_id = inbound.from_session
        channel_id = inbound.from_channel

        loop._compacting_sessions.add(session_id)
        await loop._publish_session_status_async(session_id, "compacting")

        try:
            config = loop._load_current_config()
            await loop.compact_manager.compact(
                session_id,
                channel_id,
                config=config,
                silent=False,
                trigger="manual",
            )
        except Exception:
            logger.exception(f"[command] /compact 执行异常 session={session_id}")
        finally:
            loop._compacting_sessions.discard(session_id)
            await loop._publish_session_status_async(
                session_id, loop.get_session_status(session_id)
            )
        return Handled()

    # /compress-fast：零 LLM 成本的快速压缩
    async def _on_compress_fast(ctx) -> Handled:
        inbound = ctx.meta.inbound
        session_id = inbound.from_session
        channel_id = inbound.from_channel

        try:
            config = loop._load_current_config()
            await loop.compact_manager.compress_fast(
                session_id,
                channel_id,
                config=config,
                silent=False,
            )
        except Exception:
            logger.exception(f"[command] /compress-fast 执行异常 session={session_id}")
        return Handled()

    mgr.register(
        "/cancel",
        _on_cancel,
        description="取消当前会话执行",
        system=True,
        persist_input=False,
    )
    mgr.register(
        "/compact",
        _on_compact,
        description="压缩当前会话上下文",
    )
    mgr.register(
        "/compress-fast",
        _on_compress_fast,
        description="快速压缩：裁剪旧工具输出，不调 LLM",
    )
