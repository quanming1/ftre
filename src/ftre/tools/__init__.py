"""
ftre 内置工具集

每次 build_default_tools() 创建独立的工具实例（带共享的 cwd 状态），
保证 session 之间隔离。
"""
import os

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.bus import BusMessage
from .bash import create_bash_tool, _BashState
from .edit import create_edit_tool
from .read import create_read_tool
from .think import create_think_tool
from .write import create_write_tool


def _create_send_message_tool(channel_manager) -> Tool:
    """创建 send_message 工具：向指定频道的指定 session 发送消息"""

    def send_message(
        channel_id: str,
        session_id: str,
        content: str,
        caller_channel: str = Injected("channel_id"),
        caller_session: str = Injected("session_id"),
        event_loop = Injected("event_loop"),
        bus = Injected("bus"),
        session_manager = Injected("session_manager"),
    ) -> str:
        """向指定频道的指定 session 发送一条消息"""
        import asyncio

        if channel_manager.get(channel_id) is None:
            return f"[error] 频道不存在: {channel_id}"
        if event_loop is None or bus is None or session_manager is None:
            return "[error] runtime context 未注入完整"

        msg = BusMessage(
            type="agent_event",
            from_channel=caller_channel or "",
            to_channel=channel_id,
            from_session=caller_session or "",
            to_session=session_id,
            data={"type": "message_complete", "data": {"content": content}},
        )

        try:
            # 1) 持久化到目标 session 的历史，前端切换/刷新都能看到
            asyncio.run_coroutine_threadsafe(
                session_manager.save_message(
                    session_id, "message_complete", {"content": content}
                ),
                event_loop,
            ).result(timeout=10)

            # 2) 通过 Bus outbound 分发，让 ChannelManager 推给前端（如果连接活跃）
            asyncio.run_coroutine_threadsafe(
                bus.publish_outbound(msg), event_loop
            ).result(timeout=10)

            return f"已发送到 {channel_id}:{session_id}"
        except Exception as e:
            return f"[error] 发送失败: {e}"

    return Tool(
        name="send_message",
        description="向指定频道的指定 session 发送消息。可用于跨频道通知、推送结果等。消息会同时持久化到目标 session 历史并实时推送给前端。",
        parameters=[
            ToolParameter(name="channel_id", type="string", description="目标频道 ID（如 ws、telegram）", required=True),
            ToolParameter(name="session_id", type="string", description="目标 session ID", required=True),
            ToolParameter(name="content", type="string", description="消息内容", required=True),
        ],
        func=send_message,
    )


def build_default_tools(cwd: str | None = None, channel_manager=None) -> list[Tool]:
    """构建默认工具集：think + bash + read + write + edit + send_message

    Args:
        cwd: 工作目录（默认使用进程 CWD）
        channel_manager: ChannelManager 实例（用于 send_message 工具）
    """
    state = _BashState(cwd or os.getcwd())
    tools = [
        create_think_tool(),
        create_bash_tool(state=state),
        create_read_tool(state=state),
        create_write_tool(state=state),
        create_edit_tool(state=state),
    ]

    if channel_manager:
        tools.append(_create_send_message_tool(channel_manager))

    return tools
