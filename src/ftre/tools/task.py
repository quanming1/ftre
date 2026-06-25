"""
task 工具 - 把一个提示词派发给另一个 session 同步执行（subagent 模式）

行为：
- session_id 不传 → 在 channel="subagent" 下新建 session
- session_id 传了 → 复用，会带上其历史
- 通过 SubagentChannel.receive 投递 user_message
- 投递后阻塞等待目标 session 跑完，返回最后一条 ai 回复 + session_id

终止判定：
- 在 AgentLoop 注册一个 session_id → Future[dict] 的一次性完成通知。
- AgentLoop._run 在 finally 里必定 set_result，所以无论 done 是否被发出（异常
  / 被 cancel）都能正确感知 agent 已结束。
- Future payload 中携带 status 和最后一条 message_complete 内容。

防递归：subagent channel 的调用方禁止再调 task。
"""
import asyncio
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID


# 轮询参数
_POLL_INTERVAL = 0.5         # 每 500ms 查一次状态
_STARTUP_TIMEOUT = 30        # 等 agent 启动（is_session_running 变 True）的上限
_TIMEOUT_SECONDS = 600       # 启动后到 agent 结束的上限（10 分钟）


_SUBAGENT_PREAMBLE = """\
[Subagent 上下文]
你是一个 subagent，由父 agent 通过 task 工具派发执行一个子任务。

约束：
1. 你不能调用 task 工具（subagent 内禁止再派发，会被工具层拒绝）
2. 你不能调用 send_message 工具（不要跨 session 通信，结果通过返回值给调用方即可）
3. 你不能调用 cron 工具（不要在子任务里副作用注册定时任务）

输出要求：
- 完成任务后，你的最后一条消息要清晰总结你做了什么、结论或产出
- 这条总结会作为 task 工具的返回值传给父 agent，请简洁、突出关键信息
- 如果任务无法完成，明确说明原因

实际任务如下：
"""


def _wrap_with_preamble(prompt: str) -> str:
    return _SUBAGENT_PREAMBLE + prompt


def _run_async(coro, event_loop, timeout: float = 10.0):
    """跨线程执行 coroutine 并等结果"""
    return asyncio.run_coroutine_threadsafe(coro, event_loop).result(timeout=timeout)


def _wait_until(predicate, timeout: float, interval: float = _POLL_INTERVAL) -> bool:
    """阻塞轮询 predicate，True 即返回 True；超时返回 False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def create_task_tool(channel_manager) -> Tool:
    """创建 task 工具

    Args:
        channel_manager: ChannelManager 实例。task 通过其中注册的 SubagentChannel
            投递 inbound（保持跟 ws / cron 一致的 inbound 路径）
    """

    def task(
        prompt: str,
        session_id: str = "",
        working_dir: str = "",
        caller_channel: str = Injected("channel_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        agent_loop=Injected("agent_loop"),
        workspace=Injected("workspace"),
    ) -> str:
        if not prompt or not prompt.strip():
            return "[error] prompt 不能为空"
        if event_loop is None or session_manager is None or agent_loop is None:
            return "[error] runtime context 未注入完整"
        if caller_channel == SUBAGENT_CHANNEL_ID:
            return (
                "[error] subagent 内不允许再次调用 task，"
                "避免无限递归。请直接完成任务或用 send_message 通知调用方"
            )

        subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
        if subagent_channel is None:
            return f"[error] 未注册 channel: {SUBAGENT_CHANNEL_ID}"

        sid = (session_id or "").strip()
        done_future: Future[dict] | None = None
        try:

            if not sid:
                title = prompt.strip().splitlines()[0][:40] or "subagent task"
                # 工作区优先级：显式 working_dir > 调用者当前 workspace
                caller_workspace = ""
                if working_dir.strip():
                    caller_workspace = working_dir.strip()
                else:
                    try:
                        from ._workspace import WorkspaceAccessor
                        if isinstance(workspace, WorkspaceAccessor):
                            caller_workspace = workspace.get()
                    except Exception:
                        pass
                sid = _run_async(
                    session_manager.create_session(
                        channel_id=SUBAGENT_CHANNEL_ID,
                        title=title,
                        workspace=caller_workspace,
                    ),
                    event_loop,
                )

            # 先注册完成通知再投递消息，避免 subagent 极快结束时漏掉结果。
            done_future = Future()
            if not agent_loop.register_subagent_done_future(sid, done_future):
                return (
                    f"[session={sid}, status=busy] "
                    "该 subagent session 已有一轮 task 在等待完成，请稍后重试"
                )

            wrapped_prompt = _wrap_with_preamble(prompt)

            _run_async(
                subagent_channel.receive(
                    session_id=sid,
                    data={"content": wrapped_prompt, "session_id": sid},
                ),
                event_loop,
            )
        except Exception as e:
            if sid and done_future is not None:
                agent_loop.unregister_subagent_done_future(sid, done_future)
            return f"[error] 派发失败: {type(e).__name__}: {e}"

        head = f"[session={sid}"

        # 阶段 A：验证 AgentLoop 已经消费 inbound，并进入本轮 _run。
        # 这里仍检查 running，用来区分“已投递但没有启动”和“正在执行/已完成”。
        # 极快完成时可能没轮询到 running，但 future 已经有结果，也视为启动成功。
        if not _wait_until(
            lambda: agent_loop.is_session_running(sid) or done_future.done(),
            _STARTUP_TIMEOUT,
        ):
            agent_loop.unregister_subagent_done_future(sid, done_future)
            return (
                f"{head}, status=startup_timeout] "
                f"派发后 {_STARTUP_TIMEOUT}s 内 agent 仍未启动，可能 AgentLoop "
                f"消费阻塞或鉴权失败"
            )

        # 阶段 B：等待 AgentLoop._run finally 设置完成结果，不依赖 done 事件。
        # done_payload 是 task 的唯一结果来源，final_content 不再回查 DB。
        try:
            done_payload = done_future.result(timeout=_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            agent_loop.unregister_subagent_done_future(sid, done_future)
            return (
                f"{head}, status=timeout] "
                f"任务超时（{_TIMEOUT_SECONDS}s）未完成。"
                f"可下次调用 task 时传入 session_id={sid} 接着上次执行"
            )

        # agent 已结束，使用 AgentLoop 回传的最后一条 message_complete。
        status = done_payload.get("status") or "completed"
        final_content = done_payload.get("final_content") or ""
        head_full = f"{head}, status={status}]"
        if final_content:
            return f"{head_full}\n{final_content}"
        return f"{head_full} 任务结束但 subagent 未输出最终回复（可能仅工具调用 / 异常退出）"

    return Tool(
        name="task",
        description=(
            "把一个提示词派发给一个独立 session 同步执行，等其跑完后返回最后一条 ai 回复。\n"
            "\n"
            "用法：\n"
            "- 不传 session_id：新建一个 channel='subagent' 的会话，返回值首行包含新建的 session_id\n"
            "- 传 session_id：复用该会话（带上其历史），用于让 subagent 接着上一次 task 的上下文继续\n"
            "\n"
            "重要：session_id 不能自己编造！只能从 task 工具上一次的返回值中复制粘贴。\n"
            "  返回值首行格式：[session=<sid>, status=<...>]，sid 就是这次 task 的 session_id。\n"
            "  下一次想接着同一个 subagent 对话时，把这个 sid 原样填回 session_id 参数即可。\n"
            "\n"
            "工作区继承：\n"
            "- 新建 subagent session 时默认继承调用者当前的工作区目录\n"
            "- 可通过 working_dir 参数显式指定（绝对路径），覆盖默认继承\n"
            "- subagent 内的 bash/read/write 等工具开箱即用，无需再 cd 或 set_workspace\n"
            "\n"
            "其它说明：\n"
            "- 阻塞调用：会等到目标 session 一轮跑完才返回（最长 10 分钟超时）\n"
            "- 适合拆解大任务交给独立 agent，避免污染当前会话上下文\n"
        ),
        parameters=[
            ToolParameter(
                name="prompt",
                type="string",
                description="要派发给目标 session 的提示词（user 消息）",
                required=True,
            ),
            ToolParameter(
                name="session_id",
                type="string",
                description=(
                    "目标 session ID。留空则新建一个 subagent session 并在返回值中告知新 sid。"
                    "严禁自己编造 sid；只能填上一次 task 调用返回值首行 [session=...] 里的那个 sid，"
                    "用于让 subagent 在已有上下文上继续工作"
                ),
                required=False,
            ),
            ToolParameter(
                name="working_dir",
                type="string",
                description=(
                    "subagent 的工作区目录（绝对路径）。留空则自动继承调用者当前工作区。"
                    "仅在新建 session 时生效；复用已有 session 时忽略（已有 session 保留自己的工作区）"
                ),
                required=False,
            ),
        ],
        func=task,
    )
