"""
send_message 工具 - 向另一个 session 发送一条消息

两种模式（kind 参数）：

- notify（默认）：仅通知，不触发对方 agent
  目标 session 收到一条 external_message 事件，持久化到历史 + 实时推送给前端，
  目标自己的运行不受影响。适合"通知/抄送/把结果同步给别人"。

- invoke：唤起，触发对方 agent 执行
  以 user_input 形式投递到目标 session 的 Channel.receive()，等价于"模拟一条
  用户输入"，AgentLoop 会跑起目标 agent。适合"委派任务/请求别人帮忙"。
  调用方拿不到结果（fire-and-forget），需要 await 对方回复请改用 task 工具。

通用约束：
- subagent 不允许 send_message（结果应通过 task 返回值传回）
- 不能给当前 session 自己发（直接在回复里输出即可）
"""
import asyncio

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.bus import BusMessage


# kind=invoke 时拼到 content 头部的来源标注模板
_INVOKE_PREFIX_TEMPLATE = (
    "[来自 channel={from_channel} session={from_session}]\n\n{content}"
)


def create_send_message_tool(channel_manager) -> Tool:
    """创建 send_message 工具"""

    def send_message(
        channel_id: str,
        session_id: str,
        content: str,
        kind: str = "notify",
        caller_channel: str = Injected("channel_id"),
        caller_session: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        bus=Injected("bus"),
        session_manager=Injected("session_manager"),
    ) -> str:
        # ── 通用前置校验 ────────────────────────────────────
        if caller_channel == "subagent":
            return (
                "[error] subagent 内不允许调用 send_message，"
                "请通过你的最后一条消息总结结果给调用方"
            )
        target_channel = channel_manager.get(channel_id)
        if target_channel is None:
            return f"[error] 频道不存在: {channel_id}"
        if event_loop is None:
            return "[error] runtime context 未注入完整"
        if channel_id == caller_channel and session_id == caller_session:
            return "[error] 不能给当前 session 发消息（直接在回复中输出即可）"
        if kind not in ("notify", "invoke"):
            return f"[error] 未知 kind: {kind!r}（应为 'notify' 或 'invoke'）"

        # ── 分发 ────────────────────────────────────────────
        try:
            if kind == "notify":
                if bus is None or session_manager is None:
                    return "[error] runtime context 未注入完整"
                _do_notify(
                    bus=bus,
                    session_manager=session_manager,
                    event_loop=event_loop,
                    target_channel_id=channel_id,
                    target_session_id=session_id,
                    content=content,
                    caller_channel=caller_channel or "",
                    caller_session=caller_session or "",
                )
                return f"已通知 {channel_id}:{session_id}"

            # kind == "invoke"
            _do_invoke(
                target_channel=target_channel,
                event_loop=event_loop,
                target_session_id=session_id,
                content=content,
                caller_channel=caller_channel or "",
                caller_session=caller_session or "",
            )
            return f"已唤起 {channel_id}:{session_id}"
        except Exception as e:
            return f"[error] 发送失败: {type(e).__name__}: {e}"

    return Tool(
        name="send_message",
        description=(
            "向另一个 session 发送一条消息。\n"
            "kind='notify'（默认）：仅通知，目标 session 收到一条 external_message，"
            "目标自身运行不受影响。\n"
            "kind='invoke'：唤起目标 session，以 user_input 形式触发对方 agent 执行；"
            "调用方拿不到结果（如需等待回复请改用 task 工具）。\n"
            "subagent 内禁止调用，禁止发给当前 session 自己。"
        ),
        parameters=[
            ToolParameter(name="channel_id", type="string", description="目标频道 ID（如 ws、telegram）", required=True),
            ToolParameter(name="session_id", type="string", description="目标 session ID", required=True),
            ToolParameter(name="content", type="string", description="消息内容", required=True),
            ToolParameter(
                name="kind",
                type="string",
                description="'notify' 仅通知（默认）；'invoke' 唤起目标 session 执行",
                required=False,
            ),
        ],
        func=send_message,
    )


# ============================================================
# notify 路径：external_message 事件（持久化 + outbound）
# 维持原有行为：直接 save_message + publish_outbound，
# 后续若要重构为 Channel.receive 统一入口再单独动这块。
# ============================================================

def _do_notify(
    *,
    bus,
    session_manager,
    event_loop,
    target_channel_id: str,
    target_session_id: str,
    content: str,
    caller_channel: str,
    caller_session: str,
) -> None:
    event_data = {
        "content": content,
        "from_session": caller_session,
        "from_channel": caller_channel,
    }
    msg = BusMessage(
        type="agent_event",
        from_channel=caller_channel,
        to_channel=target_channel_id,
        from_session=caller_session,
        to_session=target_session_id,
        data={"type": "external_message", "data": event_data},
    )
    # 1) 持久化到目标 session 历史，前端切换/刷新可见
    asyncio.run_coroutine_threadsafe(
        session_manager.save_message(target_session_id, "external_message", event_data),
        event_loop,
    ).result(timeout=10)
    # 2) outbound 推送给前端（连接活跃时实时显示）
    asyncio.run_coroutine_threadsafe(
        bus.publish_outbound(msg), event_loop
    ).result(timeout=10)


# ============================================================
# invoke 路径：通过目标 Channel.receive() 投递一条 user_input
# AgentLoop 会自动持久化 USER_INPUT 并 echo 给目标 session 前端。
# ============================================================

def _do_invoke(
    *,
    target_channel,
    event_loop,
    target_session_id: str,
    content: str,
    caller_channel: str,
    caller_session: str,
) -> None:
    # 来源归因：唯一让被唤起 agent 知道"这是别的 session 发来的"的方式，
    # 是把出处写进它能看见的内容里（LLM 不会读 metadata）。
    prefixed_content = _INVOKE_PREFIX_TEMPLATE.format(
        from_channel=caller_channel or "(unknown)",
        from_session=caller_session or "(unknown)",
        content=content,
    )
    data = {
        "content": prefixed_content,
        "session_id": target_session_id,
    }
    asyncio.run_coroutine_threadsafe(
        target_channel.receive(target_session_id, data, kind="user_input"),
        event_loop,
    ).result(timeout=10)
