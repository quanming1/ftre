"""SessionService —— Session 业务门面。

存储依赖 SessionRepository（纯 CRUD / 索引 / 原子提交 / 锁），
业务规则（token 用量 / 前端投影 / 启动修复）直接实现在本模块；Inbox pending
由独立 Package 持有。

对外唯一入口，方法签名与历史版本一致；调用方不应直接读写 state.json，
也不应绕过本门面直接使用 repository。它拥有 Session 身份和消息历史，但不拥有
Inbox pending 或 AgentLoop 的执行任务。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ftre_agent.message import Msg, MsgName
from ftre_agent.types import ReplyFinishedReason

from ftre.kernel.hooks import HookRuntime
from ftre.services.session.entity.models import (
    ExternalSessionModel,
    MessageModel,
    SessionModel,
    StatePageModel,
)
from ftre.services.session.entity.state import AgentStateFile, SessionState
from ftre.services.session.hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from ftre.services.session.persistence.repository import SessionRepository
from ftre.services.session.projection import SessionProjection

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class ForkResult:
    """创建分叉 Session 后返回的新身份和工作区信息。"""
    fork_session_id: str
    title: str
    workspace: str


class SessionService:
    """Session 持久化唯一入口；调用方不应直接读写 state.json。

    Repository 只负责 CRUD、索引、锁和原子提交；这里维护业务规则及 Hook/投影
    绑定。``close()`` 不删除数据，shutdown 只解除运行时引用，保证重复关闭安全。
    """
    key = "sessions"

    def __init__(
        self,
        db_path: str | None = None,
        *,
        sessions_dir: str | None = None,
        hook_runtime: HookRuntime | None = None,
    ):
        self._repo = SessionRepository(db_path, sessions_dir=sessions_dir)
        # 流式 Reply 投影属于 Session 数据面；把它放在 SessionService 内部，
        # 避免 Agent Runtime 变成 WebSocket 的第二个 Projection Owner。
        self._projection = SessionProjection(self)
        # Session 是 lifecycle Hook 的语义 Owner；Agent Runtime 不再通过 setter
        # 把回调塞进来。未接入 Runtime 的嵌入式 Session 仍可持久化，
        # 只是没有运行时观察者。
        self._hook_runtime = hook_runtime

    async def _emit_lifecycle(
        self, kind: str, session_id: str, channel_id: str = ""
    ) -> None:
        if self._hook_runtime is None:
            return
        spec = SESSION_CREATED_SPEC if kind == "created" else SESSION_DISPOSED_SPEC
        await self._hook_runtime.dispatch(
            spec,
            SessionLifecyclePayload(session_id, channel_id),
        )

    async def search_sessions(
        self,
        q: str,
        limit: int = 30,
        workspace: str | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按关键字检索会话标题与正文（内存态直接扫描，线程池执行不阻塞）。"""
        from ftre.services.session.search import search_sessions

        snapshot = self._repo.all_states()
        return await asyncio.to_thread(search_sessions, snapshot, q, limit, workspace, offset)

    async def init(self) -> None:
        """启动：加载全部 JSON 状态、建索引、修复遗留 open reply、清扫孤儿目录。"""
        await self._repo.init()
        await self._fix_open_replies()
        await self._sweep_orphan_session_dirs()

    async def close(self) -> None:
        """安全幂等：保留磁盘数据并清理流式投影内存状态。"""
        await self._projection.close()

    @property
    def projection(self) -> SessionProjection:
        """Return the Session-owned live projection used by adapters and Agent Runtime."""
        return self._projection

    async def finish_open_replies(
        self,
        session_id: str,
        reason: ReplyFinishedReason,
        *,
        error: dict[str, Any] | None = None,
    ) -> list[Msg]:
        """结束并持久化当前 Session 的 open replies。

        Projection 仍是 Session 内部实现；Runtime 只调用这个窄入口，确保异常、
        取消和 Gateway 关闭时的最终消息更新走同一把锁与同一套持久化规则。
        """
        return await self._projection.finish_open(session_id, reason, error=error)

    # ============================================================
    # Session CRUD（委托 storage）
    # ============================================================

    def create_id(self) -> str:
        """生成符合 Session 目录安全约束的新 ID。"""
        return self._repo.create_id()

    def session_dir(self, session_id: str) -> Path:
        """Session 目录（state.json 所在目录），同步纯路径计算。"""
        return self._repo.state_path(session_id).parent

    async def create_session(
        self, channel_id: str, title: str = "", workspace: str = ""
    ) -> str:
        """创建 Session，并在持久化成功后发出 created lifecycle Hook。"""
        session_id = await self._repo.create_session(channel_id, title, workspace)
        await self._emit_lifecycle("created", session_id, channel_id)
        return session_id

    async def get_or_create_external_session(
        self,
        channel_id: str,
        external_key: str,
        title: str = "",
        workspace: str = "",
        external_data: dict[str, Any] | None = None,
    ) -> str:
        """按 Channel 外部 key 幂等取得内部 Session。"""
        return await self._repo.get_or_create_external_session(
            channel_id, external_key, title, workspace, external_data
        )

    async def get_external_session(self, session_id: str) -> ExternalSessionModel | None:
        """读取外部会话绑定投影。"""
        return await self._repo.get_external_session(session_id)

    async def get_session(self, session_id: str) -> SessionModel | None:
        """读取 Session 元信息投影。"""
        return await self._repo.get_session(session_id)

    def has_session(self, session_id: str) -> bool:
        """同步只读存在性查询，供 Inbox Plugin 做 admission 校验。"""
        return self._repo.get_state(session_id) is not None

    def sessions_root(self) -> Path:
        """返回 Host 用户数据根，供需要持久化同一生命周期数据的 Package 使用。"""
        return self._repo.sessions_root()

    def has_request_id(self, session_id: str, request_id: str) -> bool:
        """同步只读幂等查询，供 Inbox 避免重复接纳已提交输入。"""
        return self._repo.has_request_id(session_id, request_id)

    def request_state(
        self, session_id: str, request_id: str, run_id: str | None = None
    ) -> str | None:
        """同步查询请求是否已经产生过 Assistant 执行结果。"""
        return self._repo.request_state(session_id, request_id, run_id)

    async def update_session(
        self,
        session_id: str,
        title: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """更新标题/工作区等 Session 元信息，不修改消息历史。"""
        await self._repo.update_session(session_id, title, workspace)

    async def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        """读取可扩展 Session metadata 的防御性副本。"""
        return await self._repo.get_session_metadata(session_id)

    async def update_session_metadata(
        self, session_id: str, key: str, value: Any | None
    ) -> dict[str, Any]:
        """替换 metadata 的一个 key，并返回更新后的 metadata。"""
        return await self._repo.update_session_metadata(session_id, key, value)

    async def mutate_session_metadata(
        self, session_id: str, key: str, updater
    ) -> dict[str, Any]:
        """原子读-改-写 metadata 的单个 key（updater(旧值) -> 新值，全程在锁内）。"""
        return await self._repo.mutate_session_metadata(session_id, key, updater)

    async def append_command_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> int:
        """Persist one Command lifecycle record without projecting it as chat content."""
        if not isinstance(event, dict) or not event.get("type"):
            raise ValueError("command event must contain a type")
        def append(old):
            records = list(old) if isinstance(old, list) else []
            records.append(copy.deepcopy(event))
            return records

        metadata = await self.mutate_session_metadata(
            session_id,
            "_command_events",
            append,
        )
        return len(metadata.get("_command_events") or [])

    async def get_command_events(self, session_id: str) -> list[dict[str, Any]]:
        """Return the durable Command lifecycle log for diagnostics/replay."""
        metadata = await self.get_session_metadata(session_id)
        events = metadata.get("_command_events")
        return copy.deepcopy(events) if isinstance(events, list) else []

    async def delete_session(self, session_id: str) -> None:
        """删除 session。

        若是 team leader：级联取消并删除全部成员 session 与 sub_agents profile 树。
        若是 team 成员（被单独删除）：反向从 leader 的 teams 摘除并删其 profile。
        """
        from ftre.services.agent_profile import (
            sub_agent as sub_agent_profile,  # 惰性导入避免包间循环
        )

        meta = await self.get_session_metadata(session_id)  # 不存在 → {}，幂等入口

        # 1) 收集受影响 session：自身 +（若是 leader）全部成员
        member_sids: list[str] = []
        teams = meta.get("teams")
        if isinstance(teams, dict):
            for team in teams.values():
                if isinstance(team, dict) and isinstance(team.get("members"), dict):
                    member_sids.extend(
                        k for k in team["members"] if isinstance(k, str)
                    )

        # 删成员 session（成员不能再建团队，级联深度恒为 1，无环）。
        # 运行时关闭由 AgentLoop.delete_session 先完成，Manager 只负责持久化删除。
        for msid in member_sids:
            await self._repo.delete_session(msid)
            await self._emit_lifecycle("disposed", msid)

        # 4) 删 leader 的 sub_agents 整棵树（含未登记的残留目录）
        sub_agent_profile.delete_all_profiles(self, session_id)

        # 5) 删自身
        await self._repo.delete_session(session_id)
        await self._emit_lifecycle("disposed", session_id)

        # 6) 反向解绑：被单独删除的是 team 成员时，从 leader 的 teams 摘除
        binding = sub_agent_profile.binding_of(meta)
        if binding is not None:
            await self._unbind_member_from_leader(
                binding["leader_session"], binding.get("team_id", ""), session_id
            )
            sub_agent_profile.delete_member_profile(
                self, binding["leader_session"], session_id
            )

    async def _unbind_member_from_leader(
        self, leader_sid: str, team_id: str, member_sid: str
    ) -> None:
        """从 leader 的 metadata['teams'][team_id].members 摘除成员（原子 RMW）。

        leader 不存在/团队不存在时静默 no-op。
        """
        def _remove(old):
            teams_now = old if isinstance(old, dict) else {}
            team_now = teams_now.get(team_id)
            if isinstance(team_now, dict) and isinstance(team_now.get("members"), dict):
                team_now["members"].pop(member_sid, None)
            return teams_now

        try:
            await self._repo.mutate_session_metadata(leader_sid, "teams", _remove)
        except ValueError:
            pass  # leader session 已不存在

    async def _sweep_orphan_session_dirs(self) -> None:
        """删除 sessions/ 下的孤儿目录：既无 state.json、又非损坏隔离件、
        且目录名不属于任何已加载 session。典型来源：旧版本删除 leader 后
        遗留的 sub_agents 树。"""
        known_ids = {sid for sid, _ in self._repo.all_states()}
        try:
            root = self._repo.sessions_root()
            children = sorted(root.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name in known_ids:
                continue
            if (child / "state.json").exists():
                continue  # 有正式文件却未加载 → 异常态，不动
            if list(child.glob("state.json.corrupt-*")):
                continue  # 损坏隔离件，保留取证
            shutil.rmtree(child, ignore_errors=True)
            logger.warning("[session-store] 清理孤儿 session 目录: %s", child)

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> list[SessionModel]:
        """分页列出 Session 元信息，可按 Channel/工作区过滤。"""
        return await self._repo.list_sessions(limit, offset, channel_id, workspace)

    async def count_sessions(
        self,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> int:
        """统计过滤条件下的 Session 数量。"""
        return await self._repo.count_sessions(channel_id, workspace)

    async def list_workspaces(self, channel_id: str | None = None) -> list[dict]:
        """返回工作区聚合列表，供工作区选择器使用。"""
        return await self._repo.list_workspaces(channel_id)

    # ============================================================
    # Message（委托 storage）
    # ============================================================

    async def save_message(
        self,
        session_id: str,
        message: Msg | dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> str:
        """新增一条 Msg 快照；重复 ID 由 Repository 拒绝。"""
        return await self._repo.save_message(session_id, message, timestamp=timestamp)

    async def update_message(self, message: Msg | dict[str, Any]) -> None:
        """更新已存在的 Msg 快照，保持消息 ID 不变。"""
        await self._repo.update_message(message)

    async def update_messages(
        self, messages: list[Msg | dict[str, Any]]
    ) -> None:
        """批量原子更新同一 Session 的多条 Msg。"""
        await self._repo.update_messages(messages)

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """按历史顺序读取 Session 的持久消息。"""
        return await self._repo.get_messages_by_session(session_id)

    async def upsert_message(
        self, session_id: str, message: Msg | dict[str, Any]
    ) -> str:
        """按 Msg ID 幂等新增或更新消息，供 Projection 重放使用。"""
        return await self._repo.upsert_message(session_id, message)

    # ============================================================
    # 上下文裁剪（给 LLM 的上下文窗口与按轮分页）
    # ============================================================

    def build_user_content(
        self,
        content: Any,
        attachments: list[dict[str, Any]] | None,
        *,
        include_images: bool = True,
    ) -> str | list[dict[str, Any]]:
        """把用户输入与附件组装成 OpenAI 安全的 content。

        消息格式转换是 Session wire 的一部分（多模态 parts、附件 data URL）；
        Agent Runtime（ftre-agent-runtime）不 import 本模块，只调用该窄方法。
        """
        from ftre.services.session.message.multimodal import build_user_content

        return build_user_content(content, attachments, include_images=include_images)

    def normalize_stored_user_content(self, content: Any) -> list[dict[str, Any]]:
        """归一持久化存储的用户 content parts（纯函数窄出口）。"""
        from ftre.services.session.message.multimodal import (
            normalize_stored_user_content,
        )

        return normalize_stored_user_content(content)

    def to_openai_messages(
        self,
        records: list[MessageModel] | tuple[MessageModel, ...],
        *,
        vision: bool,
    ) -> list[dict[str, Any]]:
        """把持久化 Msg 记录转换为 provider 消息列表。

        与 Runtime 的约定：``vision`` 决定图片 parts 是否进入请求。转换规则
        由 session.message 模块唯一持有，防止 Runtime 内出现第二份 wire 逻辑。
        """
        from ftre.services.session.message.converter import to_openai

        return to_openai(list(records), config={"llm": {"vision": vision}})

    def record_to_msg(self, record: MessageModel | Msg | dict[str, Any]) -> Msg:
        """把一条持久化记录还原为 typed Msg（确认恢复路径使用）。"""
        from ftre.services.session.message.converter import _as_msg

        return _as_msg(record)

    async def get_context_messages(self, session_id: str) -> list[MessageModel]:
        """返回给 LLM 使用的上下文消息：最后一条 compact Msg 及其后的全部消息。

        无 compact Msg 时返回全部 messages。compact Msg（role=user, name=compact）
        本身包含在返回结果中——它之前的原始消息已被摘要覆盖，不再加载。

        tail 起点由最后一条 compact Msg 的 ``through_message_id`` 决定（而非
        compact Msg 在数组中的位置），这样 compact 期间到达、但先于 compact Msg
        写入的新消息不会丢失。无 ``through_message_id`` 时退化为 compact Msg 之后。

        上下文裁剪完全由本方法完成；converter 不再做二次 clear。
        """
        state = self._repo.get_state(session_id)
        if state is None:
            return []
        messages = state.messages
        last_compact_idx = -1
        for index, message in enumerate(messages):
            if message.name == MsgName.COMPACT:
                last_compact_idx = index
        if last_compact_idx < 0:
            return [self._repo.to_message_model(m, session_id) for m in messages]
        compact_msg = messages[last_compact_idx]
        through_id = (
            (compact_msg.metadata.get("context_compact") or {}).get("through_message_id", "")
        )
        # 找 through_id 对应位置；不存在时退化为 compact Msg 之后
        through_idx = -1
        if through_id:
            for index, message in enumerate(messages):
                if message.id == through_id:
                    through_idx = index
                    break
        start = through_idx if through_idx >= 0 else last_compact_idx
        result = [compact_msg]
        for message in messages[start + 1:]:
            if message.name != MsgName.COMPACT:
                result.append(message)
        return [self._repo.to_message_model(m, session_id) for m in result]

    async def get_recent_messages_by_turns(
        self, session_id: str, limit_turns: int = 5, before_ts: float | None = None
    ) -> tuple[list[MessageModel], bool]:
        """获取指定 session 最近 N 轮对话的所有消息。

        一轮 = 一条可见 user Msg，到下一条可见 user Msg（或末尾）之间的消息。

        Args:
            limit_turns: 返回最近 N 轮
            before_ts: 可选游标，只考虑 timestamp < before_ts 的消息（用于加载更早）

        Returns:
            (messages, has_more): messages 按时间正序；has_more 表示是否还有更早的消息。
        """
        records = await self._repo.get_messages_by_session(session_id)
        if before_ts is not None:
            records = [r for r in records if r["timestamp"] < before_ts]
        if not records:
            return [], False

        # 从后向前找最近 limit_turns 条可见 UserMsg
        visible_user_indexes = [
            index
            for index, record in enumerate(records)
            if record["role"] == "user" and not record["metadata"].get("hide", False)
        ]
        if not visible_user_indexes:
            return [], False

        target = visible_user_indexes[-limit_turns:]
        start = target[0]
        messages = records[start:]

        # compact Msg 虽然是隐藏的 user Msg（它不能成为一个 turn 的边界），但它是
        # 当前上下文的锚点，也是客户端刷新后恢复“已压缩”气泡所需的唯一历史记录。
        # 当当前页从 compact 之后的新 turn 开始时，把页首之前最近的一条补回来即可；
        # 更早的 compact 已经被这条新的摘要覆盖，不需要一并返回。
        latest_compact_before_page = next(
            (
                record
                for record in reversed(records[:start])
                if record.get("name") == "compact"
            ),
            None,
        )
        if latest_compact_before_page is not None:
            messages.insert(0, latest_compact_before_page)
        has_more = start > 0
        return messages, has_more

    # ============================================================
    # Token 用量（最近一次 LLM 实算 + 之后未计入消息的字符级粗估）
    # ============================================================

    async def get_token_usage(self, session_id: str) -> dict:
        """
        计算指定 session 当前 token 用量。

        对 get_context_messages()（summary + tail）计算，而不是完整
        transcript——否则 compact 后原始消息仍在 messages，统计会一直
        认为上下文没有缩小。

        策略：
        - 找上下文中最晚的"携带 token.last_call_usage 的 assistant Msg"作为锚点
        - 锚点之后还没被 LLM 计入但会进下次 prompt 的 Msg 用字符级粗估
        - total = last_call_usage.total_tokens + pending_estimated
        - 没有锚点时（全新 session）退化为对全量上下文估算
        """
        messages = await self.get_context_messages(session_id)
        return _compute_token_usage(session_id, messages)

    # ============================================================
    # 前端投影（state.json 分页只读视图，供 Inspector / HTTP API）
    # ============================================================

    async def get_state_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 50,
        max_string_chars: int = 20_000,
    ) -> StatePageModel | None:
        """读取 state.json 的一致性分页快照。

        ``offset=None`` 默认返回最后一页，适合 Inspector 首次打开时快速查看
        当前状态；传入 offset 可继续向前分页。读取与写入共用 per-session 锁，
        因而不会把不同版本的 session / messages / metadata 拼在一起。
        """
        async with self._repo.lock_for(session_id):
            state = self._repo.get_state(session_id)
            if state is None:
                return None

            total = len(state.messages)
            role_counts = {"user": 0, "assistant": 0, "system": 0}
            block_counts = {
                "text": 0,
                "thinking": 0,
                "tool_call": 0,
                "tool_result": 0,
                "data": 0,
            }
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            latest_model: str | None = None
            for message in state.messages:
                if message.role in role_counts:
                    role_counts[message.role] += 1
                model = message.metadata.get("model")
                if isinstance(model, str) and model:
                    latest_model = model
                if message.token is not None:
                    prompt_tokens += message.token.usage.prompt_tokens
                    completion_tokens += message.token.usage.completion_tokens
                    total_tokens += message.token.usage.total_tokens
                for block in message.content:
                    block_type = getattr(block, "type", "")
                    if block_type in block_counts:
                        block_counts[block_type] += 1
            page_limit = max(1, min(limit, 100))
            page_offset = (
                max(0, total - page_limit)
                if offset is None
                else max(0, min(offset, total))
            )
            end = min(total, page_offset + page_limit)
            messages: list[dict[str, Any]] = []
            truncated_message_ids: list[str] = []
            for message in state.messages[page_offset:end]:
                payload = message.model_dump(mode="json")
                compacted, truncated = _truncate_large_strings(
                    payload,
                    max_chars=max(1_000, min(max_string_chars, 100_000)),
                )
                messages.append(compacted)
                if truncated:
                    truncated_message_ids.append(message.id)
            return {
                "schema_version": state.schema_version,
                "file_path": str(self._repo.state_path(session_id)),
                "session": state.session.model_dump(mode="json"),
                "messages": messages,
                "metadata": state.metadata.copy(),
                "truncated_message_ids": truncated_message_ids,
                "stats": {
                    "message_count": total,
                    "user_messages": role_counts["user"],
                    "assistant_messages": role_counts["assistant"],
                    "system_messages": role_counts["system"],
                    "text_blocks": block_counts["text"],
                    "thinking_blocks": block_counts["thinking"],
                    "tool_calls": block_counts["tool_call"],
                    "tool_results": block_counts["tool_result"],
                    "data_blocks": block_counts["data"],
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "model": latest_model,
                },
                "page": {
                    "offset": page_offset,
                    "limit": page_limit,
                    "total": total,
                    "has_more_before": page_offset > 0,
                    "has_more_after": end < total,
                },
            }

    async def get_state_message(
        self,
        session_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """按需读取 state.json 中一条完整 Msg，供分页视图展开超大内容。"""
        async with self._repo.lock_for(session_id):
            state = self._repo.get_state(session_id)
            if state is None:
                return None
            for message in state.messages:
                if message.id == message_id:
                    return message.model_dump(mode="json")
            return None

    # ============================================================
    # Fork：基于父 session 派生一个独立副本
    # ============================================================

    # fork 复制的是对话内容；以下 key 是「活资源的所有权/身份绑定」，
    # 只能属于原 session，fork 一律不继承：
    # - teams:       团队关系（含活跃成员 session 引用，复制会产生悬空引用，
    #                fork 上 team_delete 会误删原 leader 的成员）
    # - team_member: 成员身份绑定（fork 不能冒充原成员）
    # - external:    外部平台会话绑定（复制会让两个 session 抢占同一外部会话索引）
    FORK_METADATA_EXCLUDE = frozenset({"teams", "team_member", "external"})

    async def fork_session(self, parent_session_id: str) -> ForkResult:
        """把 parent_session_id 派生为一个新的独立 session（单次原子落盘）。

        fork 出的新 session 沿用父的 channel/workspace，完整复制父的 messages
        （每条 Msg 重新生成 id，避免与父 session 的 Msg.id 冲突），并深拷贝父的
        metadata，额外追加 forked_from / forked_at 溯源信息。

        性能：整份新 state 在内存一次性构建后单次 commit 落盘（1 次序列化 +
        1 次 fsync + 1 次 replace），不再逐条 save_message / 逐 key 写 metadata
        （原实现每条消息/每个 key 都全量写盘，O(N²)）。

        并发：阶段 1 在父 session 锁内做一次性一致快照后释放；阶段 2 在锁外组装
        完整 state；阶段 3 由 repository 在 global_lock 内提交。父锁与 global_lock
        顺序获取、绝不嵌套——禁止把阶段 1 挪进 global_lock 临界区，也禁止持父锁时
        获取 global_lock，否则与 delete_session 的 global→session 嵌套构成死锁。

        Raises:
            ValueError: 父 session 不存在。
        """
        # ── 阶段 1：父锁内一次性一致快照（messages + metadata 同 commit 点）──
        async with self._repo.lock_for(parent_session_id):
            parent_state = self._repo.get_state(parent_session_id)
            if parent_state is None:
                raise ValueError(f"session not found: {parent_session_id}")

            parent_header = parent_state.session
            fork_id = self._repo.make_session_id(parent_header.channel_id)
            fork_title = (
                f"fork of {parent_header.title}"
                if parent_header.title
                else f"fork of {parent_session_id}"
            )
            fork_workspace = parent_header.workspace
            parent_agent_id = parent_header.agent_id
            parent_channel_id = parent_header.channel_id
            now = _now_iso()
            # 关键：每条 Msg 重新生成 id，避免跨 session 的 Msg.id 冲突
            # （content/created_at 等其余字段原样保留，数组顺序与父一致）。
            cloned_messages = [
                msg.model_copy(deep=True, update={"id": _gen_msg_id()})
                for msg in parent_state.messages
            ]
            # 业务规则留在 manager：活资源所有权键不继承（FORK_METADATA_EXCLUDE）
            fork_metadata = {
                key: copy.deepcopy(value)
                for key, value in parent_state.metadata.items()
                if key not in self.FORK_METADATA_EXCLUDE
            }

        # ── 阶段 2：锁外组装完整 state（全新构建，绝不 model_copy 父 state——
        # 否则会连带继承 Inbox/external/teams/id/时间戳）──
        fork_metadata["forked_from"] = parent_session_id
        fork_metadata["forked_at"] = datetime.now(UTC).isoformat()
        new_state = AgentStateFile(
            session=SessionState(
                id=fork_id,
                agent_id=parent_agent_id,
                channel_id=parent_channel_id,
                title=fork_title,
                workspace=fork_workspace,
                created_at=now,
                updated_at=now,
            ),
            messages=cloned_messages,
            metadata=fork_metadata,
        )

        # ── 阶段 3：单次原子落盘（repository 内部持 global_lock）──
        await self._repo.create_session_with_state(new_state)

        return ForkResult(
            fork_session_id=fork_id,
            title=fork_title,
            workspace=fork_workspace,
        )

    # ============================================================
    # 启动期恢复：修复 Gateway 重启前未完成的 Reply
    # ============================================================

    async def _fix_open_replies(self) -> None:
        """将所有 finished_at is null 的 assistant Msg 标记为 interrupted。

        Gateway 重启后 LLM 调用无法恢复，进行中 Reply 必须标记终态
        使 state.json 自洽，客户端不会看到永远 streaming 的消息。
        """
        now = _now_iso()
        fixed = 0
        for session_id, state in self._repo.all_states():
            dirty = False
            for msg in state.messages:
                if msg.role == "assistant" and msg.finished_at is None:
                    msg.finished_at = now
                    msg.finished_reason = ReplyFinishedReason.INTERRUPTED
                    msg.error = {
                        "code": "gateway_restarted",
                        "message": "Gateway restarted before this reply completed.",
                    }
                    fixed += 1
                    dirty = True
            if dirty:
                await self._repo.commit(state)
        if fixed:
            logger.warning(
                "[session-store] 修复 %d 条遗留 open reply (标记为 interrupted)",
                fixed,
            )

    # Msg → provider 消息的转换统一位于 message/converter.py。


def _gen_msg_id() -> str:
    """与 ftre_agent Msg 的 id 生成规则保持一致（uuid4 hex 前 16 位）。"""
    return uuid.uuid4().hex[:16]


def _truncate_large_strings(value: Any, *, max_chars: int) -> tuple[Any, bool]:
    """递归压缩超长字符串，避免 state 分页被单个 base64/tool output 撑大。"""
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value, False
        omitted = len(value) - max_chars
        return (
            (f"{value[:max_chars]}\n"
            f"… <省略 {omitted} 个字符，展开后可加载完整消息>"),
            True,
        )
    if isinstance(value, list):
        output = []
        truncated = False
        for item in value:
            compacted, item_truncated = _truncate_large_strings(
                item, max_chars=max_chars,
            )
            output.append(compacted)
            truncated = truncated or item_truncated
        return output, truncated
    if isinstance(value, dict):
        output = {}
        truncated = False
        for key, item in value.items():
            compacted, item_truncated = _truncate_large_strings(
                item, max_chars=max_chars,
            )
            output[key] = compacted
            truncated = truncated or item_truncated
        return output, truncated
    return value, False


def _find_last_call_usage(messages: list[MessageModel]) -> tuple[int, dict | None]:
    """倒序找最晚的带 token.last_call_usage 的 assistant Msg。"""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message.get("role") != "assistant":
            continue
        token = message.get("token")
        if not token:
            continue
        last_call = token.get("last_call_usage")
        if (
            isinstance(last_call, dict)
            and {"prompt_tokens", "completion_tokens", "total_tokens"}.issubset(last_call)
        ):
            return i, last_call
    return -1, None


def _compute_token_usage(session_id: str, messages: list[MessageModel]) -> dict:
    """
    根据 Msg 快照计算 token 用量。抽出来便于单测，不依赖存储。

    - 找上下文中最晚的"携带 token.last_call_usage 的 assistant Msg"作为锚点
    - 锚点之后还没被 LLM 计入但会进下次 prompt 的 Msg 用字符级粗估
    - total = last_call_usage.total_tokens + pending_estimated
    - 没有锚点时（全新 session）退化为对全量上下文估算
    """
    from .message.token_counter import estimate_messages_tokens

    anchor_index, last_call_usage = _find_last_call_usage(messages)

    # 锚点之后的消息用字符级粗估（无锚点时即全量估算）
    pending_messages = messages[anchor_index + 1:] if anchor_index >= 0 else messages
    pending_estimated = estimate_messages_tokens(pending_messages)

    if last_call_usage is None:
        return {
            "session_id": session_id,
            "last_call_usage": None,
            "pending_estimated": pending_estimated,
            "total": pending_estimated,
        }

    return {
        "session_id": session_id,
        "last_call_usage": {
            "prompt_tokens": int(last_call_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(last_call_usage.get("completion_tokens") or 0),
            "total_tokens": int(last_call_usage.get("total_tokens") or 0),
        },
        "pending_estimated": pending_estimated,
        "total": int(last_call_usage.get("total_tokens") or 0) + pending_estimated,
    }
