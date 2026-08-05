"""
team 工具集 — 让主 agent（leader）组建并管理一个由多个 subagent 组成的团队。

与 task 工具的关系：
- task：把一个 prompt 派给一个 subagent，**同步阻塞**等它跑完拿结果。
- team：把 subagent 升格为可长期存活、**异步执行**的团队成员。leader 派活后
  立即返回、不阻塞，之后用 team_agent_peep 查状态、wait_agent 批量等完成。

核心模型：
- 一个「团队」持久化在**发起 team 的父 session** 的 metadata['teams'] 下，
  一个父 session 可创建多个团队（team_id 为键）。
- 一个「成员 agent」就是一个 channel=subagent 的独立 session（与 task 同源）；
  成员的系统提示词（sys_prompt）存在成员 session 的 metadata['team_sys_prompt']，
  由 before_agent_run hook 注入。
- 成员以 **session_id 为键** 存在 team.members 下。team_say / team_agent_peep /
  wait_agent 全部按 session_id 定位成员。

数据结构（父 session metadata['teams']）：
{
  "<team_id>": {
    "id": "<team_id>",
    "name": "销售分析组",
    "created_at": "ISO8601",
    "members": {
      "<member_session_id>": {
        "name": "数据分析师",
        "sys_prompt": "你是……",
        "created_at": "ISO8601"
      }
    }
  }
}

防递归：team 工具禁止在 subagent channel 内调用（成员不能再建子团队）。
"""
import asyncio
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID


# ── 成员首次触发消息前言 + 约束 ────────────────────────────────
# 成员的角色定义走 metadata['team_sys_prompt']（真正的 system prompt），
# 这里只放一条随首次触发消息下发的任务上下文说明。
_MEMBER_KICKOFF = """\
[团队成员上下文]
你是一个团队成员 agent，由团队 leader 通过 team 工具创建并派发任务。
你的角色与职责见系统提示词。请完成 leader 交给你的任务，
在最后一条消息里清晰总结你的产出或结论——leader 会通过查看你的最新状态来获取它。
"""


def _run_async(coro, event_loop, timeout: float = 10.0):
    """跨线程执行 coroutine 并等结果。"""
    return asyncio.run_coroutine_threadsafe(coro, event_loop).result(timeout=timeout)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 轮询参数（wait_agent 用，与 task 对齐）
_POLL_INTERVAL = 0.5
_STARTUP_TIMEOUT = 30


def _wait_until(predicate, timeout: float, interval: float = _POLL_INTERVAL) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _read_teams(session_manager, event_loop, parent_sid: str) -> dict:
    """读父 session 的 metadata['teams']（副本），无则返回 {}。"""
    metadata = _run_async(
        session_manager.get_session_metadata(parent_sid), event_loop
    )
    teams = metadata.get("teams")
    return teams if isinstance(teams, dict) else {}


def _write_teams(session_manager, event_loop, parent_sid: str, teams: dict) -> None:
    """整体回写父 session 的 metadata['teams']。"""
    _run_async(
        session_manager.update_session_metadata(parent_sid, "teams", teams),
        event_loop,
    )


def _member_last_text(session_manager, event_loop, member_sid: str) -> str | None:
    """读成员 session 已持久化的最后一条 assistant 的最后一个 TextBlock 文本。"""
    records = _run_async(
        session_manager.get_messages_by_session(member_sid), event_loop
    )
    for rec in reversed(records or []):
        if rec.get("role") != "assistant":
            continue
        content = rec.get("content") or []
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        texts = [t for t in texts if t]
        if texts:
            return texts[-1]
    return None


def create_team_tools(channel_manager) -> list[Tool]:
    """构建 team 工具集，返回 6 个 Tool。

    Args:
        channel_manager: ChannelManager 实例，通过其中的 SubagentChannel 投递 inbound。
    """
    return [
        _create_team_create_tool(),
        _create_team_add_agent_tool(channel_manager),
        _create_team_say_tool(channel_manager),
        _create_team_agent_peep_tool(),
        _create_team_delete_tool(),
        _create_wait_agent_tool(),
    ]


def _guard_not_subagent(caller_channel: str) -> str | None:
    """team 工具禁止在 subagent 内调用（防成员再建子团队）。"""
    if caller_channel == SUBAGENT_CHANNEL_ID:
        return (
            "[error] 团队成员（subagent）内不允许调用 team 工具，"
            "不能创建子团队或管理团队。请专注完成 leader 交给你的任务。"
        )
    return None


# ── team_create ────────────────────────────────────────────────
def _create_team_create_tool() -> Tool:
    def team_create(
        team_name: str,
        team_id: str = "",
        session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        if not session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_name or not team_name.strip():
            return "[error] team_name 不能为空"

        tid = (team_id or "").strip() or f"team_{uuid.uuid4().hex[:8]}"
        teams = _read_teams(session_manager, event_loop, session_id)
        if tid in teams:
            return f"[error] team_id 已存在: {tid}"
        teams[tid] = {
            "id": tid,
            "name": team_name.strip(),
            "created_at": _now_iso(),
            "members": {},
        }
        _write_teams(session_manager, event_loop, session_id, teams)
        return (
            f"已创建团队 '{team_name.strip()}'（team_id={tid}）。\n"
            f"用 team_add_agent(team_id='{tid}', agent_name=..., sys_prompt=...) 添加成员。"
        )

    return Tool(
        name="team_create",
        description=(
            "创建一个团队（挂在当前会话下，可创建多个）。你作为 leader 通过团队"
            "组织多个成员 agent 并行协作。\n"
            "- 创建后用 team_add_agent 往团队里添加成员 agent（每个成员是一个独立、"
            "异步执行的 subagent）。\n"
            "- 返回值包含 team_id，后续所有 team 操作都要用它。"
        ),
        parameters=[
            ToolParameter(
                name="team_name", type="string",
                description="团队名称，如『销售分析组』", required=True,
            ),
            ToolParameter(
                name="team_id", type="string",
                description="可选，自定义团队 ID；不传则自动生成。",
                required=False,
            ),
        ],
        func=team_create,
    )


# ── team_add_agent ─────────────────────────────────────────────
def _create_team_add_agent_tool(channel_manager) -> Tool:
    def team_add_agent(
        team_id: str,
        agent_name: str,
        sys_prompt: str,
        session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        caller_channel: str = Injected("channel_id"),
        workspace=Injected("workspace"),
    ) -> str:
        if not session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_id.strip() or not agent_name.strip() or not sys_prompt.strip():
            return "[error] team_id / agent_name / sys_prompt 均不能为空"

        teams = _read_teams(session_manager, event_loop, session_id)
        team = teams.get(team_id.strip())
        if team is None:
            return f"[error] 团队不存在: {team_id}"

        subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
        if subagent_channel is None:
            return f"[error] 未注册 channel: {SUBAGENT_CHANNEL_ID}"

        # 成员工作区继承 leader 当前工作区
        member_workspace = ""
        try:
            from ._workspace import WorkspaceAccessor
            if isinstance(workspace, WorkspaceAccessor):
                member_workspace = workspace.get()
        except Exception:
            pass

        try:
            member_sid = _run_async(
                session_manager.create_session(
                    channel_id=SUBAGENT_CHANNEL_ID,
                    title=agent_name.strip(),
                    workspace=member_workspace,
                ),
                event_loop,
            )
            # sys_prompt 作为成员系统提示词（由 before_agent_run hook 注入）
            _run_async(
                session_manager.update_session_metadata(
                    member_sid, "team_sys_prompt", sys_prompt.strip()
                ),
                event_loop,
            )
        except Exception as e:
            return f"[error] 创建成员 session 失败: {type(e).__name__}: {e}"

        # 登记成员到父 team（session_id 为键）
        team.setdefault("members", {})[member_sid] = {
            "name": agent_name.strip(),
            "sys_prompt": sys_prompt.strip(),
            "created_at": _now_iso(),
        }
        _write_teams(session_manager, event_loop, session_id, teams)

        # 异步投递首次触发消息：不注册 done_future、不等待
        try:
            _run_async(
                subagent_channel.receive(
                    session_id=member_sid,
                    data={"content": _MEMBER_KICKOFF, "session_id": member_sid},
                ),
                event_loop,
            )
        except Exception as e:
            return (
                f"成员已创建（session_id={member_sid}）但首次触发投递失败："
                f"{type(e).__name__}: {e}。可用 team_say 手动派活。"
            )

        return (
            f"已添加成员 '{agent_name.strip()}' 到团队 {team_id}，"
            f"session_id={member_sid}，正在后台执行。\n"
            f"用 team_agent_peep(team_id='{team_id}', session_id='{member_sid}') 查看进展，"
            f"或 wait_agent 等它完成。"
        )

    return Tool(
        name="team_add_agent",
        description=(
            "向团队添加一个成员 agent 并立即派活（异步，不阻塞）。\n"
            "- 会创建一个独立的成员 session，把 sys_prompt 设为该成员的系统提示词"
            "（定义它的角色、职责、产出要求），随后成员在后台开始执行。\n"
            "- 立即返回成员 session_id；不会等待成员完成。\n"
            "- sys_prompt 要写清楚：成员是谁、要做什么、约束、期望产出格式。"
            "成员看不到你的对话历史，所需上下文全部写进 sys_prompt。\n"
            "- 添加多个成员后用 wait_agent 批量等待，用 team_agent_peep 查看中间状态。"
        ),
        parameters=[
            ToolParameter(
                name="team_id", type="string",
                description="目标团队 ID（team_create 返回的）", required=True,
            ),
            ToolParameter(
                name="agent_name", type="string",
                description="成员名称/角色，如『数据分析师』", required=True,
            ),
            ToolParameter(
                name="sys_prompt", type="string",
                description=(
                    "成员的系统提示词：定义它的角色、职责、任务、约束、产出格式。"
                    "必须自包含——成员没有你的对话历史，完成任务所需的一切都要写进来，"
                    "路径用绝对路径。"
                ),
                required=True,
            ),
        ],
        func=team_add_agent,
    )


# ── team_say ───────────────────────────────────────────────────
def _create_team_say_tool(channel_manager) -> Tool:
    def team_say(
        team_id: str,
        session_id: str,
        content: str,
        parent_session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        agent_loop=Injected("agent_loop"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        if not parent_session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_id.strip() or not session_id.strip() or not content.strip():
            return "[error] team_id / session_id / content 均不能为空"

        teams = _read_teams(session_manager, event_loop, parent_session_id)
        team = teams.get(team_id.strip())
        if team is None:
            return f"[error] 团队不存在: {team_id}"
        member = (team.get("members") or {}).get(session_id.strip())
        if member is None:
            return f"[error] 成员不存在于团队 {team_id}: {session_id}"

        # 成员正忙则直接返回，让 leader 自行控流（可 wait_agent 后再 say）
        if agent_loop is not None and agent_loop.is_session_running(session_id.strip()):
            return (
                f"[busy] 成员 '{member.get('name')}'（{session_id}）正在执行上一轮任务，"
                f"暂时无法接收新消息。请先用 wait_agent 等它完成，再 team_say。"
            )

        subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
        if subagent_channel is None:
            return f"[error] 未注册 channel: {SUBAGENT_CHANNEL_ID}"

        try:
            _run_async(
                subagent_channel.receive(
                    session_id=session_id.strip(),
                    data={"content": content, "session_id": session_id.strip()},
                ),
                event_loop,
            )
        except Exception as e:
            return f"[error] 投递失败: {type(e).__name__}: {e}"

        return (
            f"已向成员 '{member.get('name')}'（{session_id}）派发消息，正在后台处理。"
            f"用 wait_agent 等待完成，或 team_agent_peep 查看进展。"
        )

    return Tool(
        name="team_say",
        description=(
            "给团队某个成员发送一条消息/派发新任务（异步，不阻塞）。\n"
            "- 按成员 session_id 定位（team_add_agent 返回的那个 id）。\n"
            "- 若成员正在执行上一轮任务，返回 [busy]，此时你应先用 wait_agent 等它完成再发。\n"
            "- 投递后立即返回，成员在后台处理；用 wait_agent / team_agent_peep 获取结果。"
        ),
        parameters=[
            ToolParameter(
                name="team_id", type="string",
                description="成员所属团队 ID", required=True,
            ),
            ToolParameter(
                name="session_id", type="string",
                description="目标成员的 session_id（team_add_agent 返回的）",
                required=True,
            ),
            ToolParameter(
                name="content", type="string",
                description="要发给该成员的消息内容（新任务或补充指令）",
                required=True,
            ),
        ],
        func=team_say,
    )


# ── team_agent_peep ────────────────────────────────────────────
def _create_team_agent_peep_tool() -> Tool:
    def team_agent_peep(
        team_id: str,
        session_id: str,
        parent_session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        agent_loop=Injected("agent_loop"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        if not parent_session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_id.strip() or not session_id.strip():
            return "[error] team_id / session_id 不能为空"

        teams = _read_teams(session_manager, event_loop, parent_session_id)
        team = teams.get(team_id.strip())
        if team is None:
            return f"[error] 团队不存在: {team_id}"
        member = (team.get("members") or {}).get(session_id.strip())
        if member is None:
            return f"[error] 成员不存在于团队 {team_id}: {session_id}"

        running = (
            agent_loop is not None
            and agent_loop.is_session_running(session_id.strip())
        )
        status = "running" if running else "idle"
        last_text = _member_last_text(session_manager, event_loop, session_id.strip())

        head = (
            f"<FTRE_SYSTEM_FACT>[member={member.get('name')}, "
            f"session={session_id}, status={status}]</FTRE_SYSTEM_FACT>"
        )
        if last_text:
            return f"{head}\n{last_text}"
        if running:
            return f"{head}\n成员正在执行，尚无已完成的输出。"
        return f"{head}\n成员暂无输出（可能还未开始或仅有工具调用）。"

    return Tool(
        name="team_agent_peep",
        description=(
            "查看团队某个成员的最新执行状态（不阻塞）。\n"
            "- 返回该成员是否正在运行，以及它已持久化的最后一段文字输出"
            "（该成员最近一轮的最终回复）。\n"
            "- 注意：只反映已落盘的结果，成员正在流式生成的内容不会实时显示。"
            "要拿到成员本轮的最终产出，用 wait_agent 等它完成后再 peep。"
        ),
        parameters=[
            ToolParameter(
                name="team_id", type="string",
                description="成员所属团队 ID", required=True,
            ),
            ToolParameter(
                name="session_id", type="string",
                description="目标成员的 session_id", required=True,
            ),
        ],
        func=team_agent_peep,
    )


# ── team_delete ────────────────────────────────────────────────
def _create_team_delete_tool() -> Tool:
    def team_delete(
        team_id: str,
        session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        if not session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_id.strip():
            return "[error] team_id 不能为空"

        teams = _read_teams(session_manager, event_loop, session_id)
        team = teams.pop(team_id.strip(), None)
        if team is None:
            return f"[error] 团队不存在: {team_id}"

        # 级联删除所有成员 session
        member_sids = list((team.get("members") or {}).keys())
        deleted, failed = 0, 0
        for msid in member_sids:
            try:
                _run_async(session_manager.delete_session(msid), event_loop)
                deleted += 1
            except Exception:
                failed += 1

        _write_teams(session_manager, event_loop, session_id, teams)
        msg = f"已解散团队 {team_id}（'{team.get('name')}'），删除 {deleted} 个成员 session。"
        if failed:
            msg += f" {failed} 个成员 session 删除失败（可能已不存在）。"
        return msg

    return Tool(
        name="team_delete",
        description=(
            "解散一个团队并级联删除其所有成员 session（不可恢复）。\n"
            "- 团队任务全部完成、不再需要时调用，回收资源。\n"
            "- 会删除该团队下每个成员对应的 subagent session 及其历史。"
        ),
        parameters=[
            ToolParameter(
                name="team_id", type="string",
                description="要解散的团队 ID", required=True,
            ),
        ],
        func=team_delete,
    )


# ── wait_agent ─────────────────────────────────────────────────
def _create_wait_agent_tool() -> Tool:
    def wait_agent(
        session_ids: list = None,
        parent_session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        agent_loop=Injected("agent_loop"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        if not parent_session_id or event_loop is None or agent_loop is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        sids = [str(s).strip() for s in (session_ids or []) if str(s).strip()]
        if not sids:
            return "[error] session_ids 不能为空"

        # 为每个 session 注册 done_future（已完成/未运行的直接跳过等待）
        futures: dict[str, Future] = {}
        for sid in sids:
            fut: Future = Future()
            if not agent_loop.register_subagent_done_future(sid, fut):
                # 已有等待者：不重复等，标记为 skipped
                futures[sid] = None
                continue
            futures[sid] = fut

        # 逐个等待（Promise.all 效果）：先确认已启动或已完成，再取结果
        results: list[str] = []
        for sid in sids:
            fut = futures.get(sid)
            if fut is None:
                results.append(f"- {sid}: [skipped] 已有其他等待者，未重复等待")
                continue

            # 若该 session 既没在跑也没结果，说明它可能已完成上一轮，直接跳过等待
            if not agent_loop.is_session_running(sid) and not fut.done():
                # 短暂等待确认是否真的没有任务在跑
                started = _wait_until(
                    lambda: agent_loop.is_session_running(sid) or fut.done(),
                    2.0,
                )
                if not started:
                    agent_loop.unregister_subagent_done_future(sid, fut)
                    last = _member_last_text(session_manager, event_loop, sid)
                    results.append(
                        f"- {sid}: [idle] 当前无任务在跑。"
                        + (f"最后输出：{last}" if last else "无输出。")
                    )
                    continue

            try:
                payload = fut.result()
            except Exception as e:
                agent_loop.unregister_subagent_done_future(sid, fut)
                results.append(f"- {sid}: [error] 等待出错: {type(e).__name__}: {e}")
                continue

            status = payload.get("status") or "completed"
            # 与 team_agent_peep 保持一致：返回成员最后一条 assistant 消息的
            # 最后一个 text block 完整内容，不截断、不用可能被截断的 final_content
            final = _member_last_text(session_manager, event_loop, sid)
            snippet = final if final else "（无最终文本输出）"
            results.append(f"- {sid}: [{status}] {snippet}")

        return "wait_agent 完成，各成员结果：\n" + "\n".join(results)

    return Tool(
        name="wait_agent",
        description=(
            "等待一批成员 agent 完成当前任务后再继续（类似 Promise.all）。\n"
            "- 传入若干成员 session_id，阻塞直到它们各自跑完当前这一轮，"
            "然后一次性返回每个成员的完成状态与最终输出摘要。\n"
            "- 已经空闲（无任务在跑）的成员会立即返回其最后输出，不会死等。\n"
            "- 典型用法：team_add_agent 派出多个成员后，用 wait_agent 等它们全部完成，"
            "再汇总结果或用 team_say 派下一轮。"
        ),
        parameters=[
            ToolParameter(
                name="session_ids", type="array",
                description="要等待的成员 session_id 列表（team_add_agent 返回的那些 id）",
                required=True,
            ),
        ],
        func=wait_agent,
    )
