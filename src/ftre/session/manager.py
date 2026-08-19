"""SessionManager —— Session 业务门面。

存储依赖 SessionRepository（纯 CRUD / 索引 / 原子提交 / 锁），
业务规则（上下文裁剪 / token 用量 / 前端投影 / 启动修复）直接实现在本模块。

对外唯一入口，方法签名与历史版本一致；调用方不应直接读写 state.json，
也不应绕过本门面直接使用 repository。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ftre_agent_core.message import Msg, MsgName
from ftre_agent_core.types import ReplyFinishedReason

from ftre.bus import BusMessage

from ftre.session.entity.models import (
    ExternalSessionModel,
    MessageModel,
    SessionModel,
    StatePageModel,
)
from ftre.session.storage.repository import SessionRepository
from ftre.session.entity.state import (
    AgentStateFile,
    MailboxState,
    QueueItem,
    SessionState,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class ForkResult:
    fork_session_id: str
    title: str
    workspace: str


@dataclass(frozen=True)
class RequestAdmission:
    """一条入站消息的持久化接纳结果（存储层语义）。

    由 ``SessionManager.admit_inbound()`` 返回，回答"这次接纳尝试发生了什么"：
    - ``created`` 为 False 表示 request_id 已在 pending 或 UserMessage 中出现；
    - ``queue_position`` 是 pending 中的 1-based 位置，已领取的历史请求为 0。
    """

    session_id: str
    request_id: str
    created: bool
    queue_position: int


class SessionManager:
    """Session 持久化唯一入口；调用方不应直接读写 state.json。"""

    def __init__(self, db_path: str | None = None, *, sessions_dir: str | None = None):
        self._repo = SessionRepository(db_path, sessions_dir=sessions_dir)

    async def search_sessions(
        self,
        q: str,
        limit: int = 30,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """按关键字检索会话标题与正文（内存态直接扫描，线程池执行不阻塞）。"""
        from ftre.session.search import search_sessions

        snapshot = self._repo.all_states()
        return await asyncio.to_thread(search_sessions, snapshot, q, limit, workspace)

    async def init(self) -> None:
        """启动：加载全部 JSON 状态、建索引、修复遗留 open reply、清扫孤儿目录。"""
        await self._repo.init()
        await self._fix_open_replies()
        await self._sweep_orphan_session_dirs()

    async def close(self) -> None:
        """安全幂等：JSON Store 无长连接，仅清理内存状态。"""
        # 幂等 no-op：保留磁盘状态，重复调用安全
        return None

    # ============================================================
    # Session CRUD（委托 storage）
    # ============================================================

    def create_id(self) -> str:
        return self._repo.create_id()

    def session_dir(self, session_id: str) -> Path:
        """Session 目录（state.json 所在目录），同步纯路径计算。"""
        return self._repo.state_path(session_id).parent

    async def create_session(
        self, channel_id: str, title: str = "", workspace: str = ""
    ) -> str:
        return await self._repo.create_session(channel_id, title, workspace)

    async def get_or_create_external_session(
        self,
        channel_id: str,
        external_key: str,
        title: str = "",
        workspace: str = "",
        external_data: dict[str, Any] | None = None,
    ) -> str:
        return await self._repo.get_or_create_external_session(
            channel_id, external_key, title, workspace, external_data
        )

    async def get_external_session(self, session_id: str) -> ExternalSessionModel | None:
        return await self._repo.get_external_session(session_id)

    async def get_session(self, session_id: str) -> SessionModel | None:
        return await self._repo.get_session(session_id)

    async def update_session(
        self,
        session_id: str,
        title: str | None = None,
        workspace: str | None = None,
    ) -> None:
        await self._repo.update_session(session_id, title, workspace)

    async def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        return await self._repo.get_session_metadata(session_id)

    async def update_session_metadata(
        self, session_id: str, key: str, value: Any | None
    ) -> dict[str, Any]:
        return await self._repo.update_session_metadata(session_id, key, value)

    async def mutate_session_metadata(
        self, session_id: str, key: str, updater
    ) -> dict[str, Any]:
        """原子读-改-写 metadata 的单个 key（updater(旧值) -> 新值，全程在锁内）。"""
        return await self._repo.mutate_session_metadata(session_id, key, updater)

    async def delete_session(self, session_id: str) -> None:
        """删除 session。

        若是 team leader：级联取消并删除全部成员 session 与 sub_agents profile 树。
        若是 team 成员（被单独删除）：反向从 leader 的 teams 摘除并删其 profile。
        """
        from ftre.agent import sub_agent_profile  # 惰性导入避免包间循环

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

        # 4) 删 leader 的 sub_agents 整棵树（含未登记的残留目录）
        sub_agent_profile.delete_all_profiles(self, session_id)

        # 5) 删自身
        await self._repo.delete_session(session_id)

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
        return await self._repo.list_sessions(limit, offset, channel_id, workspace)

    async def count_sessions(
        self,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> int:
        return await self._repo.count_sessions(channel_id, workspace)

    async def list_workspaces(self, channel_id: str | None = None) -> list[dict]:
        return await self._repo.list_workspaces(channel_id)

    # ============================================================
    # Mailbox（仅由 AgentLoop 内部的 SessionLane 使用）
    # ============================================================

    async def admit_inbound(
        self, inbound: BusMessage, *, mailbox_capacity: int = 100
    ) -> RequestAdmission:
        session_id = inbound.data.get("session_id", "") or inbound.from_session
        if not session_id:
            raise ValueError("inbound 缺少 session_id")
        session = await self.get_session(session_id)
        if session is None:
            raise ValueError(f"session 不存在: {session_id}")
        if session["channel_id"] != inbound.from_channel:
            raise ValueError(
                f"session 与 channel 不匹配: {session_id} ({session['channel_id']})"
            )

        # request_id 是请求在 mailbox、历史 UserMsg 和 WS 事件间唯一共用的业务 ID。
        # WS 使用自己的 frame_id 注入它；内部调用未给出时只在这里生成一次。
        request_id = (
            inbound.metadata.request_id
            or f"request_{uuid.uuid4().hex}"
        )
        created, position = await self._repo.admit_request(
            session_id,
            request_id=request_id,
            content=str(inbound.data.get("content") or ""),
            attachments=list(inbound.data.get("attachments") or []),
            agent_id=inbound.metadata.agent_id,
            capacity=mailbox_capacity,
        )
        return RequestAdmission(
            session_id=session_id,
            request_id=request_id,
            created=created,
            queue_position=position,
        )

    async def peek_request(self, session_id: str) -> QueueItem | None:
        return await self._repo.peek_request(session_id)

    async def take_pending_request(
        self, session_id: str, request_id: str
    ) -> QueueItem | None:
        return await self._repo.take_pending_request(session_id, request_id)

    async def cancel_pending_request(
        self, session_id: str, request_id: str
    ) -> QueueItem | None:
        """取消尚未被 SessionLane 领取的请求。"""
        return await self._repo.cancel_pending_request(session_id, request_id)

    async def get_mailbox_snapshot(self, session_id: str) -> MailboxState:
        return await self._repo.mailbox_snapshot(session_id)

    async def advance_mailbox_revision(self, session_id: str) -> int:
        """记录一次对客户端可见的 Lane 状态变化。"""
        return await self._repo.advance_mailbox_revision(session_id)

    def has_mailbox_work(self, session_id: str) -> bool:
        """同步判断该会话是否仍有 pending 或 active 请求。

        AgentLoop 的状态查询不应穿透到 Repository 私有字段；这里仅读内存快照，
        不做 I/O，也不把 Mailbox 的具体 JSON 结构泄露给调用方。
        """
        state = self._repo.get_state(session_id)
        return bool(state and state.mailbox.pending)

    async def get_pending_request_count(self, session_id: str) -> int:
        return len((await self._repo.mailbox_snapshot(session_id)).pending)

    async def get_mailbox_session_ids(self) -> list[str]:
        return await self._repo.mailbox_session_ids()

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
        return await self._repo.save_message(session_id, message, timestamp=timestamp)

    async def update_message(self, message: Msg | dict[str, Any]) -> None:
        await self._repo.update_message(message)

    async def update_messages(
        self, messages: list[Msg | dict[str, Any]]
    ) -> None:
        """批量原子更新同一 Session 的多条 Msg。"""
        await self._repo.update_messages(messages)

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        return await self._repo.get_messages_by_session(session_id)

    async def upsert_message(
        self, session_id: str, message: Msg | dict[str, Any]
    ) -> str:
        return await self._repo.upsert_message(session_id, message)

    # ============================================================
    # 上下文裁剪（给 LLM 的上下文窗口与按轮分页）
    # ============================================================

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
        # 否则会连带继承 mailbox/external/teams/id/时间戳）──
        fork_metadata["forked_from"] = parent_session_id
        fork_metadata["forked_at"] = datetime.now(timezone.utc).isoformat()
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
            mailbox=MailboxState(),  # mailbox 不继承：全新空队列
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
    """与 ftre_agent_core Msg 的 id 生成规则保持一致（uuid4 hex 前 16 位）。"""
    return uuid.uuid4().hex[:16]


def _truncate_large_strings(value: Any, *, max_chars: int) -> tuple[Any, bool]:
    """递归压缩超长字符串，避免 state 分页被单个 base64/tool output 撑大。"""
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value, False
        omitted = len(value) - max_chars
        return (
            f"{value[:max_chars]}\n"
            f"… <省略 {omitted} 个字符，展开后可加载完整消息>",
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
