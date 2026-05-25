"""
task 工具 - 把一个提示词派发给另一个 session 同步执行（subagent 模式）

行为：
- session_id 不传 → 在 channel="subagent" 下新建 session
- session_id 传了 → 复用，会带上其历史
- 通过 SubagentChannel.receive 投递 user_input，跟 ws/cron 一样走标准 channel
  路径，让 outbound 分发不报 unknown channel
- 投递后阻塞等待目标 session 跑完，返回最后一条 ai 回复 + session_id

终止判定：轮询 SessionManager.get_messages_by_session，等到 timestamp ≥ baseline
的 'done' 事件即认为完成，然后取这之后的最后一条 message_complete 作为返回值。

防递归：subagent channel 的调用方禁止再调 task。
"""
import asyncio
import logging
import time

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID


logger = logging.getLogger(__name__)


# 轮询参数
_POLL_INTERVAL = 0.5      # 每 500ms 查一次
_TIMEOUT_SECONDS = 600    # 单次 task 最长等待 10 分钟


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


def _wait_for_completion(
    session_manager,
    event_loop,
    sid: str,
    baseline_ts: float,
) -> tuple[str | None, str]:
    """
    阻塞轮询直到该 session 出现 timestamp ≥ baseline_ts 的 done 事件。

    返回 (final_content, status):
      status: 'completed' | 'timeout' | 'error'
      final_content: 该 done 之前最近一条 message_complete 的 content；
                     没有时为 None
    """
    deadline = time.time() + _TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            events = _run_async(
                session_manager.get_messages_by_session(sid),
                event_loop,
                timeout=10.0,
            )
        except Exception as e:
            logger.warning(f"[task] 轮询 events 失败: {e}")
            time.sleep(_POLL_INTERVAL)
            continue

        # 倒序找 baseline 之后的 done：跨过基线就停，无需扫全量
        done_ev = None
        for ev in reversed(events):
            ts = ev.get("timestamp", 0)
            if ts < baseline_ts:
                break
            if ev.get("type") == "done":
                done_ev = ev
                break

        if done_ev is not None:
            done_ts = done_ev.get("timestamp", 0)
            # 倒序找最近一条 message_complete（在 done 时间点之前、baseline 之后）
            final_content: str | None = None
            for ev in reversed(events):
                ts = ev.get("timestamp", 0)
                if ts > done_ts:
                    continue
                if ts < baseline_ts:
                    break
                if ev.get("type") == "message_complete":
                    final_content = (ev.get("data") or {}).get("content") or ""
                    break
            success = (done_ev.get("data") or {}).get("success", True)
            return final_content, "completed" if success else "error"

        time.sleep(_POLL_INTERVAL)

    return None, "timeout"


def create_task_tool(channel_manager) -> Tool:
    """创建 task 工具

    Args:
        channel_manager: ChannelManager 实例。task 通过其中注册的 SubagentChannel
            投递 inbound（保持跟 ws / cron 一致的 inbound 路径）
    """

    def task(
        prompt: str,
        session_id: str = "",
        caller_channel: str = Injected("channel_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
    ) -> str:
        if not prompt or not prompt.strip():
            return "[error] prompt 不能为空"
        if event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        if caller_channel == SUBAGENT_CHANNEL_ID:
            return (
                "[error] subagent 内不允许再次调用 task，"
                "避免无限递归。请直接完成任务或用 send_message 通知调用方"
            )

        subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
        if subagent_channel is None:
            return f"[error] 未注册 channel: {SUBAGENT_CHANNEL_ID}"

        try:
            sid = (session_id or "").strip()

            if not sid:
                title = prompt.strip().splitlines()[0][:40] or "subagent task"
                sid = _run_async(
                    session_manager.create_session(
                        channel_id=SUBAGENT_CHANNEL_ID,
                        title=title,
                    ),
                    event_loop,
                )

            wrapped_prompt = _wrap_with_preamble(prompt)

            # 记录派发时间作为完成事件的过滤基线（必须在 publish 前）
            baseline_ts = time.time()

            # 通过 SubagentChannel.receive 走标准 inbound 路径，跟 ws/cron 一致
            _run_async(
                subagent_channel.receive(
                    session_id=sid,
                    data={"content": wrapped_prompt, "session_id": sid},
                ),
                event_loop,
            )
        except Exception as e:
            return f"[error] 派发失败: {type(e).__name__}: {e}"

        # 阻塞等结果
        final_content, status = _wait_for_completion(
            session_manager, event_loop, sid, baseline_ts
        )

        head = f"[session={sid}, status={status}]"
        if status == "timeout":
            return (
                f"{head} 任务超时（{_TIMEOUT_SECONDS}s）未完成。"
                f"可下次调用 task 时传入 session_id={sid} 接着上次执行"
            )
        if final_content:
            return f"{head}\n{final_content}"
        # done 已到达但没有 message_complete：subagent 可能直接静默退出 / 仅工具调用
        # 没有最终总结。父 agent 应当注意这条状态。
        return f"{head} 任务结束但 subagent 未输出最终回复"

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
        ],
        func=task,
    )
