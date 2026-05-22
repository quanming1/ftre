"""
send_message 工具 - 向指定频道的指定 session 发送一条消息

行为：
- 同时持久化到目标 session 的历史（前端切换/刷新可见）
- 通过 Bus outbound 分发，ChannelManager 推送到前端（连接活跃时实时显示）
- 拒绝目标等于调用者自身（避免给当前 session 发自我消息）
"""
import asyncio

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.bus import BusMessage


def create_send_message_tool(channel_manager) -> Tool:
    """创建 send_message 工具"""

    def send_message(
        channel_id: str,
        session_id: str,
        content: str,
        caller_channel: str = Injected("channel_id"),
        caller_session: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        bus=Injected("bus"),
        session_manager=Injected("session_manager"),
    ) -> str:
        if channel_manager.get(channel_id) is None:
            return f"[error] 频道不存在: {channel_id}"
        if event_loop is None or bus is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        if channel_id == caller_channel and session_id == caller_session:
            return "[error] 不能给当前 session 发消息（直接在回复中输出即可）"

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
        description=(
            "向指定频道的指定 session 发送消息。可用于跨频道通知、推送结果等。"
            "消息会同时持久化到目标 session 历史并实时推送给前端。"
        ),
        parameters=[
            ToolParameter(name="channel_id", type="string", description="目标频道 ID（如 ws、telegram）", required=True),
            ToolParameter(name="session_id", type="string", description="目标 session ID", required=True),
            ToolParameter(name="content", type="string", description="消息内容", required=True),
        ],
        func=send_message,
    )
