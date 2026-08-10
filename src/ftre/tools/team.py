"""
team 工具集 — 让主 agent（leader）组建并管理一个由多个 subagent 组成的团队。

与 task 工具的关系：
- task：把一个 prompt 派给一个 subagent，**同步阻塞**等它跑完拿结果。
- team：把 subagent 升格为可长期存活、**异步执行**的团队成员。leader 派活后
  立即返回、不阻塞，之后用 team_agent_status 查状态、wait_agent 批量等完成。

核心模型：
- 一个「团队」持久化在**发起 team 的父 session** 的 metadata['teams'] 下，
  一个父 session 可创建多个团队（team_id 为键）。teams 只存**成员关系**
  （name / created_at），成员的配置实体在磁盘。
- 一个「成员 agent」= 一个 channel=subagent 的独立 session + 一份落盘的
  AgentProfile，格式与全局 agent（~/.ftre/agents/<id>/）完全同构：

      ~/.ftre/sessions/<leader_session_id>/sub_agents/<member_session_id>/
      ├── AGENTS.md           # 成员角色定义（team_add_agent 的 profile.role）
      ├── agent.config.json   # 可选覆盖：llm / tools / disabled_skills / mcp
      ├── SOUL.md             # 可选，手工添加
      └── USER.md             # 可选，手工添加

  成员身份双路解析（turn_executor._resolve_turn_config）：
  1. inbound 消息携带的 agent_ref（team 工具投递时附加，sub_agent 必须等于本 session）
  2. 成员 session 自身 metadata['team_member'] 结构性绑定（任意入口兜底）
  解析后走与全局 agent 完全相同的组装路径（AGENTS.md 注入、llm 覆盖等）。
  目录布局/读写/绑定形状全部收口在 ftre/agent/sub_agent_profile.py。
  teams 的写路径经 SessionRepository.mutate_session_metadata 原子读-改-写，
  并行 tool call 不丢更新。
- 成员以 **session_id 为键** 存在 team.members 下。team_say / team_agent_status /
  wait_agent 全部按 session_id 定位成员。

数据结构（父 session metadata['teams']）：
{
  "<team_id>": {
    "id": "<team_id>",
    "name": "销售分析组",
    "created_at": "ISO8601",
    "members": {
      "<member_session_id>": { "name": "数据分析师", "created_at": "ISO8601" }
    }
  }
}

防递归：team 工具禁止在 subagent channel 内调用（成员不能再建子团队），
成员 AGENTS.md 里也写入了同等约束。
"""
import asyncio
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone

from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.agent import sub_agent_profile
from ftre.agent.event_hub import AgentEventHub
from ftre.bus import AgentRef, InboundMetadata
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID


# team_add_agent 的 profile 对象允许的字段（与全局 agent.config.json 同名同义）
_PROFILE_ALLOWED_KEYS = frozenset({
    "role", "provider", "model", "reasoning_effort",
    "tools", "disabled_skills", "mcp",
})


class _TeamOpError(Exception):
    """teams 原子 updater 内的受控业务错误，穿透 run_coroutine_threadsafe 传回工具线程。"""



def _agent_ref_metadata(leader_session_id: str, member_session_id: str) -> InboundMetadata:
    """发往成员 session 的消息统一携带的 profile 定位标记。"""
    return InboundMetadata(
        agent_ref=AgentRef(
            leader_session=leader_session_id,
            sub_agent=member_session_id,
        )
    )


def _dispatch_to_member(
    channel_manager,
    event_loop,
    leader_session_id: str,
    member_session_id: str,
    content: str,
) -> None:
    """向团队成员投递一条 inbound user_message（团队内发消息的唯一入口）。

    走标准 Inbound 通路 Channel.receive(kind="user_message")——与 send_message
    的 invoke 路径同源，AgentLoop 会正常消费、持久化、驱动成员 agent。
    统一携带 agent_ref 定位标记（成员据此加载自己的 profile）。

    Raises:
        RuntimeError: subagent channel 未注册。
        其它异常由 Channel.receive 传播，调用方决定如何处理。
    """
    subagent_channel = channel_manager.get(SUBAGENT_CHANNEL_ID)
    if subagent_channel is None:
        raise RuntimeError(f"未注册 channel: {SUBAGENT_CHANNEL_ID}")
    _run_async(
        subagent_channel.receive(
            session_id=member_session_id,
            data={"content": content, "session_id": member_session_id},
            metadata=_agent_ref_metadata(leader_session_id, member_session_id),
            kind="user_message",
        ),
        event_loop,
    )


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
    """读父 session 的 metadata['teams']（副本），无则返回 {}。只读路径使用。"""
    metadata = _run_async(
        session_manager.get_session_metadata(parent_sid), event_loop
    )
    teams = metadata.get("teams")
    return teams if isinstance(teams, dict) else {}


def _mutate_teams(session_manager, event_loop, parent_sid: str, fn) -> dict:
    """原子读-改-写父 session 的 metadata['teams']。

    fn(teams 旧值) -> 新 teams，整体在 session 锁内执行，并发调用不丢更新。
    fn 内抛 _TeamOpError 时不提交，状态保持不变。
    """
    metadata = _run_async(
        session_manager.mutate_session_metadata(parent_sid, "teams", fn),
        event_loop,
    )
    teams = metadata.get("teams")
    return teams if isinstance(teams, dict) else {}


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
        _create_team_agent_status_tool(),
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

        def _create(old):
            teams = old if isinstance(old, dict) else {}
            if tid in teams:
                raise _TeamOpError(f"team_id 已存在: {tid}")
            teams[tid] = {
                "id": tid,
                "name": team_name.strip(),
                "created_at": _now_iso(),
                "members": {},
            }
            return teams

        try:
            _mutate_teams(session_manager, event_loop, session_id, _create)
        except _TeamOpError as e:
            return f"[error] {e}"
        except Exception as e:
            return f"[error] 创建团队失败: {type(e).__name__}: {e}"
        return (
            f"已创建团队 '{team_name.strip()}'（team_id={tid}）。\n"
            f"用 team_add_agent(team_id='{tid}', agent_name=..., profile={{'role': ...}}) "
            f"添加成员。"
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
        profile: dict = None,
        invoke: str = "",
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
        if not team_id.strip() or not agent_name.strip():
            return "[error] team_id / agent_name 均不能为空"
        if not isinstance(profile, dict):
            return "[error] profile 必须是对象（至少包含 role 字段）"
        role = profile.get("role")
        if not isinstance(role, str) or not role.strip():
            return "[error] profile.role 不能为空（成员的角色定义）"
        unknown = set(profile) - _PROFILE_ALLOWED_KEYS
        if unknown:
            return (
                f"[error] profile 含未知字段: {sorted(unknown)}；"
                f"允许的字段: {sorted(_PROFILE_ALLOWED_KEYS)}"
            )

        teams = _read_teams(session_manager, event_loop, session_id)
        team = teams.get(team_id.strip())
        if team is None:
            return f"[error] 团队不存在: {team_id}"

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
        except Exception as e:
            return f"[error] 创建成员 session 失败: {type(e).__name__}: {e}"

        # 成员 profile 落盘（先全量校验后写盘）：<leader session>/sub_agents/<member>/
        # 格式与全局 agent 同构（AGENTS.md + agent.config.json）
        try:
            sub_agent_profile.write_member_profile(
                session_manager, session_id, member_sid,
                role=role, overrides=profile,
            )
        except (OSError, ValueError) as e:
            try:
                _run_async(session_manager.delete_session(member_sid), event_loop)
            except Exception:
                pass
            return f"[error] 写入成员 profile 失败: {type(e).__name__}: {e}"

        # 成员 session 的结构性绑定：任意入口的消息都能解析出成员 profile
        binding = sub_agent_profile.build_team_member_binding(
            session_id, team_id.strip(), agent_name.strip()
        )
        try:
            _run_async(
                session_manager.update_session_metadata(
                    member_sid, "team_member", binding
                ),
                event_loop,
            )
        except Exception as e:
            sub_agent_profile.delete_member_profile(session_manager, session_id, member_sid)
            try:
                _run_async(session_manager.delete_session(member_sid), event_loop)
            except Exception:
                pass
            return f"[error] 写入成员绑定失败: {type(e).__name__}: {e}"

        # 原子登记成员到父 team（锁内复检团队存在，防并发删除/覆盖）
        def _register(old):
            teams_now = old if isinstance(old, dict) else {}
            team_now = teams_now.get(team_id.strip())
            if not isinstance(team_now, dict):
                raise _TeamOpError(f"团队不存在: {team_id}")
            members = team_now.get("members")
            members = members if isinstance(members, dict) else {}
            members[member_sid] = {
                "name": agent_name.strip(),
                "created_at": _now_iso(),
            }
            team_now["members"] = members
            return teams_now

        try:
            _mutate_teams(session_manager, event_loop, session_id, _register)
        except Exception as e:
            sub_agent_profile.delete_member_profile(session_manager, session_id, member_sid)
            try:
                _run_async(session_manager.delete_session(member_sid), event_loop)
            except Exception:
                pass
            return (
                f"[error] 登记成员失败（团队可能已被并发删除）: {e}。"
                f"已回滚成员 session 与 profile。"
            )

        # invoke 未传：只创建不执行，成员待命，由 team_say 派活
        if not (invoke or "").strip():
            return (
                f"已添加成员 '{agent_name.strip()}' 到团队 {team_id}，"
                f"session_id={member_sid}。成员已就绪但尚未激活，"
                f"用 team_say(team_id='{team_id}', session_id='{member_sid}', content=...) 派活。"
            )

        # invoke 已传：作为成员的第一条 user 消息，创建后立即执行
        try:
            _dispatch_to_member(
                channel_manager, event_loop, session_id, member_sid, invoke.strip()
            )
        except Exception as e:
            return (
                f"成员已创建（session_id={member_sid}）但首次触发投递失败："
                f"{type(e).__name__}: {e}。可用 team_say 手动派活。"
            )

        return (
            f"已添加成员 '{agent_name.strip()}' 到团队 {team_id}，"
            f"session_id={member_sid}，正在后台执行首个任务。\n"
            f"用 team_agent_status(team_id='{team_id}', session_id='{member_sid}') 查看进展，"
            f"或 wait_agent 等它完成。"
        )

    return Tool(
        name="team_add_agent",
        description=(
            "向团队添加一个成员 agent（异步，不阻塞）。\n"
            "- 创建独立的成员 session，并把 profile 落盘为该成员的 AgentProfile"
            "（与全局 agent 同构：role 写入 AGENTS.md，其余写入 agent.config.json）。\n"
            "- profile.role 必填：成员的角色定义（是谁、职责、约束、产出要求）。"
            "成员看不到你的对话历史，所需上下文全部写进 role。\n"
            "- profile 可选字段：provider/model/reasoning_effort（成员用什么模型）、"
            "tools（{\"allow\":[...],\"deny\":[...]} 收窄能力）、disabled_skills、mcp。\n"
            "- invoke 可选：传了就把该字符串作为成员的第一条任务消息立即执行；"
            "不传则只创建成员不执行，之后用 team_say 派活。\n"
            "- 立即返回成员 session_id；不会等待成员完成。"
            "添加多个成员后用 wait_agent 批量等待，用 team_agent_status 查看中间状态。"
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
                name="profile", type="object",
                description=(
                    "成员配置对象。role（必填）：角色定义，必须自包含——成员没有你的"
                    "对话历史，任务所需上下文、路径、产出格式都写进来。"
                    "可选：provider/model/reasoning_effort、tools、disabled_skills、mcp。"
                ),
                required=True,
            ),
            ToolParameter(
                name="invoke", type="string",
                description=(
                    "可选。成员的首个任务消息：传了则成员创建后立即执行该任务；"
                    "不传则成员只创建不执行，等待 team_say 派活。"
                ),
                required=False,
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

        # 成员 session 实体已不存在时明确报错，而不是静默 no-op
        try:
            member_session = _run_async(
                session_manager.get_session(session_id.strip()), event_loop
            )
        except Exception as e:
            return f"[error] 查询成员 session 失败: {type(e).__name__}: {e}"
        if member_session is None:
            return (
                f"[error] 成员 session 已不存在（可能已被删除）: {session_id}。"
                f"请重新 team_add_agent 添加该成员。"
            )

        # 成员正忙则直接返回，让 leader 自行控流（可 wait_agent 后再 say）
        if agent_loop is not None and agent_loop.is_session_running(session_id.strip()):
            return (
                f"[busy] 成员 '{member.get('name')}'（{session_id}）正在执行上一轮任务，"
                f"暂时无法接收新消息。请先用 wait_agent 等它完成，再 team_say。"
            )

        try:
            _dispatch_to_member(
                channel_manager, event_loop,
                parent_session_id, session_id.strip(), content,
            )
        except Exception as e:
            return f"[error] 投递失败: {type(e).__name__}: {e}"

        return (
            f"已向成员 '{member.get('name')}'（{session_id}）派发消息，正在后台处理。"
            f"用 wait_agent 等待完成，或 team_agent_status 查看进展。"
        )

    return Tool(
        name="team_say",
        description=(
            "给团队某个成员发送一条消息/派发新任务（异步，不阻塞）。\n"
            "- 按成员 session_id 定位（team_add_agent 返回的那个 id）。\n"
            "- 若成员正在执行上一轮任务，返回 [busy]，此时你应先用 wait_agent 等它完成再发。\n"
            "- 投递后立即返回，成员在后台处理；用 wait_agent / team_agent_status 获取结果。"
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


# ── team_agent_status ─────────────────────────────────────────
def _create_team_agent_status_tool() -> Tool:
    def team_agent_status(
        team_id: str,
        session_id: str = "",
        parent_session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
        agent_loop=Injected("agent_loop"),
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        """查询团队成员状态（不阻塞）。

        - 传 session_id：查单个成员（状态 + 最后输出 + 消息数）
        - 不传 session_id：列出团队所有成员的状态概览
        """
        if not parent_session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        if not team_id.strip():
            return "[error] team_id 不能为空"

        teams = _read_teams(session_manager, event_loop, parent_session_id)
        team = teams.get(team_id.strip())
        if team is None:
            return f"[error] 团队不存在: {team_id}"
        members = team.get("members") or {}

        def _one(member_sid: str) -> tuple[str, str, int, float]:
            """(status, last_text, msg_count, elapsed)"""
            running = (
                agent_loop is not None
                and agent_loop.is_session_running(member_sid)
            )
            status = "running" if running else "idle"
            last_text = _member_last_text(session_manager, event_loop, member_sid)
            # 消息计数：该成员 session 持久化的消息数（async → 提交到 loop）
            msg_count = 0
            try:
                msgs = _run_async(
                    session_manager.get_messages_by_session(member_sid),
                    event_loop,
                )
                msg_count = len(msgs) if msgs else 0
            except Exception:
                pass
            # 运行耗时：从 member 注册时间到现在的秒数
            elapsed = 0.0
            member_meta = members.get(member_sid) or {}
            created = member_meta.get("created_at") or ""
            if created:
                try:
                    from datetime import datetime
                    created_dt = datetime.fromisoformat(created)
                    from datetime import timezone
                    elapsed = (datetime.now(timezone.utc) - created_dt).total_seconds()
                except Exception:
                    pass
            return status, last_text, msg_count, elapsed

        # ── 单成员视图 ──
        if session_id.strip():
            member_sid = session_id.strip()
            if member_sid not in members:
                return f"[error] 成员不存在于团队 {team_id}: {member_sid}"
            status, last_text, msg_count, elapsed = _one(member_sid)
            head = (
                f"<FTRE_SYSTEM_FACT>[member={members[member_sid].get('name')}, "
                f"session={member_sid}, status={status}, "
                f"messages={msg_count}, elapsed={elapsed:.0f}s]</FTRE_SYSTEM_FACT>"
            )
            if last_text:
                return f"{head}\n{last_text}"
            if status == "running":
                return f"{head}\n成员正在执行，尚无已完成的输出。"
            return f"{head}\n成员暂无输出（可能还未开始或仅有工具调用）。"

        # ── 团队全景视图 ──
        if not members:
            return f"<FTRE_SYSTEM_FACT>[team={team_id}]</FTRE_SYSTEM_FACT>\n团队暂无成员。"
        lines = [f"<FTRE_SYSTEM_FACT>[team={team_id}, members={len(members)}]</FTRE_SYSTEM_FACT>"]
        for member_sid, meta in members.items():
            status, last_text, msg_count, elapsed = _one(member_sid)
            name = (meta or {}).get("name") or member_sid
            excerpt = (last_text or "").replace("\n", " ")[:80]
            lines.append(
                f"- {name} [{member_sid}] {status} "
                f"({msg_count} msgs, {elapsed:.0f}s)"
                + (f": {excerpt}" if excerpt else "")
            )
        return "\n".join(lines)

    return Tool(
        name="team_agent_status",
        description=(
            "查看团队成员的最新执行状态（不阻塞）。\n"
            "- 传 session_id：查单个成员——是否正在运行、已持久化的最后一段文字输出"
            "（最近一轮最终回复）、消息数、运行耗时。\n"
            "- 不传 session_id：列出团队全部成员的状态概览（每行一个成员："
            "名称/session/状态/消息数/耗时/最后输出摘要）。\n"
            "- 注意：只反映已落盘的结果，流式生成的内容不会实时显示。"
            "要拿成员本轮的最终产出，用 wait_agent 等它完成后再查。"
        ),
        parameters=[
            ToolParameter(
                name="team_id", type="string",
                description="成员所属团队 ID", required=True,
            ),
            ToolParameter(
                name="session_id", type="string",
                description="目标成员的 session_id；不传则列出团队所有成员", required=False,
            ),
        ],
        func=team_agent_status,
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

        # 原子弹出团队（锁内完成，防并发 add/delete 互相覆盖）
        popped: dict = {}

        def _pop(old):
            teams_now = old if isinstance(old, dict) else {}
            team_now = teams_now.pop(team_id.strip(), None)
            if team_now is None:
                raise _TeamOpError(f"团队不存在: {team_id}")
            popped["team"] = team_now
            return teams_now

        try:
            _mutate_teams(session_manager, event_loop, session_id, _pop)
        except _TeamOpError as e:
            return f"[error] {e}"
        except Exception as e:
            return f"[error] 解散团队失败: {type(e).__name__}: {e}"
        team = popped["team"]

        # 级联删除所有成员 session（门面 delete_session 内含：取消运行中 agent、
        # 按成员绑定删除其 profile 目录、反向解绑）
        member_sids = list((team.get("members") or {}).keys())
        deleted, failed = 0, 0
        for msid in member_sids:
            try:
                _run_async(session_manager.delete_session(msid), event_loop)
                deleted += 1
            except Exception:
                failed += 1
            # 双保险：绑定缺失时也确保 profile 目录被清
            sub_agent_profile.delete_member_profile(session_manager, session_id, msid)

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
        if not parent_session_id or event_loop is None or agent_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"
        guard = _guard_not_subagent(caller_channel)
        if guard:
            return guard
        sids = [str(s).strip() for s in (session_ids or []) if str(s).strip()]
        if not sids:
            return "[error] session_ids 不能为空"

        # 归属校验：只允许等待自己名下团队的成员
        teams = _read_teams(session_manager, event_loop, parent_session_id)
        known_members = {
            msid
            for team in teams.values()
            if isinstance(team, dict) and isinstance(team.get("members"), dict)
            for msid in team["members"]
        }
        unknown = [s for s in sids if s not in known_members]
        if unknown:
            return f"[error] 以下 session 不属于你的任何团队: {unknown}"

        # 为每个 session 注册完成等待（AgentEventHub 一次性 wait；已完成/未运行的直接跳过等待）
        futures: dict[str, Future] = {}
        for sid in sids:
            fut = agent_loop.events.wait(sid, AgentEventHub.AGENT_FINISHED)
            if fut is None:
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
                # 快速路径：成员从未收到过任何消息（没被派活）→ 必然空闲
                try:
                    has_messages = bool(
                        _run_async(session_manager.get_messages_by_session(sid), event_loop)
                    )
                except Exception:
                    has_messages = True
                if not has_messages:
                    agent_loop.events.unregister(sid, AgentEventHub.AGENT_FINISHED, fut)
                    results.append(f"- {sid}: [idle] 成员尚未收到过任务。")
                    continue
                # 已派活但运行态未确认：给足启动窗口（与 task 对齐），避免误报 idle
                started = _wait_until(
                    lambda: agent_loop.is_session_running(sid) or fut.done(),
                    _STARTUP_TIMEOUT,
                )
                if not started:
                    agent_loop.events.unregister(sid, AgentEventHub.AGENT_FINISHED, fut)
                    last = _member_last_text(session_manager, event_loop, sid)
                    results.append(
                        f"- {sid}: [idle] 当前无任务在跑。"
                        + (f"最后输出：{last}" if last else "无输出。")
                    )
                    continue

            try:
                payload = fut.result()
            except Exception as e:
                agent_loop.events.unregister(sid, AgentEventHub.AGENT_FINISHED, fut)
                results.append(f"- {sid}: [error] 等待出错: {type(e).__name__}: {e}")
                continue

            status = payload.get("status") or "completed"
            # 与 team_agent_status 保持一致：返回成员最后一条 assistant 消息的
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
