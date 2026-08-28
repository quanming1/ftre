"""
task 工具 - 把一个提示词派发给另一个 session 同步执行（subagent 模式）

行为：
- session_id 不传 → 在 channel="subagent" 下新建 session
- session_id 传了 → 复用，会带上其历史
- 通过 AgentService.submit 耐久接纳 user_message
- 投递后阻塞等待目标 session 跑完，返回最后一条 ai 回复 + session_id

终止判定：
- 通过 AgentService.wait(request_id) 等待本进程内的 Turn 结果。
- 同 session 有多条队列消息时，其他 Turn 的完成不会提前唤醒本次 task。

防递归：subagent channel 的调用方禁止再调 task。
"""
import asyncio
import uuid

from ftre_agent.tool import Injected, ToolDefinition, ToolParameter
from ftre_inbox.protocol import InboundMessage

from ftre.services.messaging.channel.names import SUBAGENT_CHANNEL_ID

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


def _run_async(coro, event_loop, timeout: float | None = 10.0):
    """跨线程执行 coroutine 并等结果"""
    return asyncio.run_coroutine_threadsafe(coro, event_loop).result(timeout=timeout)
def create_task_tool(channel_manager, inbox) -> ToolDefinition:
    """创建 task 工具

    该工厂属于 ``ftre-task``；Inbox 只是它用于耐久投递和等待的公开依赖。

    Args:
        channel_manager: ChannelManager 实例。task 通过其中注册的 SubagentChannel
            投递 inbound（保持跟 ws / cron 一致的 inbound 路径）
    """

    def task(
        prompt: str,
        session_id: str = "",
        working_dir: str = "",
        caller_channel: str = Injected("channel_id"),
        event_loop=Injected("event_loop"),  # noqa: B008 - ToolDefinition execution context primitive
        session_manager=Injected("sessions"),  # noqa: B008 - public SessionService runtime key
        agent_service=Injected("agent"),  # noqa: B008 - public AgentService runtime key
        workspace=Injected("workspace"),  # noqa: B008 - public WorkspaceAccessor runtime key
    ) -> str:
        if not prompt or not prompt.strip():
            return "[error] prompt 不能为空"
        if event_loop is None or session_manager is None or agent_service is None:
            return "[error] runtime context 未注入完整"
        if inbox is None:
            return "[error] Inbox Service 未就绪，无法派发 subagent"
        if caller_channel == SUBAGENT_CHANNEL_ID:
            return (
                "[error] subagent 内不允许再次调用 task，"
                "避免无限递归。请直接完成任务或用 send_message 通知调用方"
            )

        subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
        if subagent_channel is None:
            return f"[error] 未注册 channel: {SUBAGENT_CHANNEL_ID}"

        sid = (session_id or "").strip()
        ack = None
        try:

            if not sid:
                title = prompt.strip().splitlines()[0][:40] or "subagent task"
                # 工作区优先级：显式 working_dir > 调用者当前 workspace
                caller_workspace = ""
                if working_dir.strip():
                    caller_workspace = working_dir.strip()
                else:
                    try:
                        if hasattr(workspace, "get"):
                            caller_workspace = workspace.get()
                    except Exception:  # noqa: BLE001, S110 legacy compatibility boundary reviewed in F1
                        pass
                sid = _run_async(
                    session_manager.create_session(
                        channel_id=SUBAGENT_CHANNEL_ID,
                        title=title,
                        workspace=caller_workspace,
                    ),
                    event_loop,
                )

            wrapped_prompt = _wrap_with_preamble(prompt)

            ack = _run_async(
                inbox.followup(InboundMessage(
                    session_id=sid,
                    request_id=f"task_{uuid.uuid4().hex}",
                    channel_id=SUBAGENT_CHANNEL_ID,
                    content=wrapped_prompt,
                    source="plugin",
                )),
                event_loop,
            )
            if not ack.accepted:
                return f"[error] subagent 消息接纳失败: {ack.error}"
        except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            return f"[error] 派发失败: {type(e).__name__}: {e}"

        # 按本次 request 精确等待。CompletionRegistry 有有限内存缓存，
        # 即使 Turn 在 submit 返回后极快完成也不会丢唤醒；Gateway 重启则本次
        # task 调用本身也会中断，不尝试跨进程恢复。
        try:
            done_payload = _run_async(
                inbox.wait(sid, ack.request_id),
                event_loop,
                timeout=None,
            )
        except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            return f"[error] 等待 subagent 完成时出错: {type(e).__name__}: {e}"

        # AgentService 已返回最后一条完整 assistant Msg。
        status = done_payload.status
        final_content = done_payload.final_content
        head_full = f"<FTRE_SYSTEM_FACT>[session={sid}, status={status}]</FTRE_SYSTEM_FACT>"
        if final_content:
            return f"{head_full}\n{final_content}"
        return f"{head_full}\n任务结束但 subagent 未输出最终回复（可能仅工具调用 / 异常退出）"

    return ToolDefinition(
        name="task",
        description=(
            "把一个提示词派发给一个独立 session 同步执行，等其跑完后返回最后一条 ai 回复。\n"
            "\n"
            "用法：\n"
            "- 不传 session_id：新建一个 channel='subagent' 的会话，返回值首行包含新建的 session_id\n"
            "- 传 session_id：复用该会话（带上其历史），用于让 subagent 接着上一次 task 的上下文继续\n"
            "\n"
            "重要：session_id 不能自己编造！只能从 task 工具上一次的返回值中复制粘贴。\n"
            "  返回值首行格式：<FTRE_SYSTEM_FACT>[session=<sid>, status=<...>]</FTRE_SYSTEM_FACT>，sid 就是这次 task 的 session_id。\n"
            "  下一次想接着同一个 subagent 对话时，把这个 sid 原样填回 session_id 参数即可。\n"
            "\n"
            "工作区继承：\n"
            "- 新建 subagent session 时默认继承调用者当前的工作区目录\n"
            "- 可通过 working_dir 参数显式指定（绝对路径），覆盖默认继承\n"
            "- subagent 内的 bash/read/write 等工具开箱即用，无需再 cd 或 set_workspace\n"
            "\n"
            "其它说明：\n"
            "- 阻塞调用：会等到目标 session 一轮跑完才返回（无超时限制）\n"
            "- 适合拆解大任务交给独立 agent，避免污染当前会话上下文\n"
            "\n"
            "【关键】上下文必须自包含，prompt 要写详细：\n"
            "- subagent 是一个全新的 agent，完全没有你当前会话的任何上下文：它不知道你和用户聊过什么、\n"
            "  不知道之前定位到的文件、不知道你脑子里的项目背景，也看不到你的对话历史。\n"
            "- 因此 prompt 里要把完成任务所需的一切都讲清楚：背景、目标、约束、验收标准、相关文件、\n"
            "  已知信息、期望的输出格式。宁可啰嗦也不要假设它能猜到。\n"
            "- 一律使用绝对路径，不要用相对路径。subagent 的工作区可能和你不同，相对路径会指向错误位置；\n"
            "  涉及文件 / 目录 / 命令时都写成绝对路径（如 E:\\\\ftre\\\\src\\\\ftre\\\\tools\\\\task.py）。\n"
            "- 不要引用只有你知道的指代（如『刚才那个函数』『上面提到的配置』），把它们展开成具体内容。\n"
            "- 如果任务依赖某些前置事实（版本、环境、约定），直接在 prompt 里写明，别让 subagent 去猜。\n"
        ),
        parameters=[
            ToolParameter(
                name="prompt",
                type="string",
                description=(
                    "要派发给目标 session 的提示词（user 消息）。"
                    "必须自包含：subagent 没有你的任何上下文（看不到对话历史、不知道项目背景、"
                    "不知道你定位过的文件），所以要把背景、目标、约束、验收标准、相关文件全部写清楚。"
                    "所有路径用绝对路径而非相对路径，避免因工作区不同而指错位置；"
                    "不要使用模糊的代词，不要省略主语，全部展开成具体内容"
                ),
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
