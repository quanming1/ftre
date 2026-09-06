"""SessionService —— Session 业务门面（SessionLog 架构，PRD-F43）。

数据面（PRD-F43）：消息事实的唯一载体是 per-session 事件日志
``sessions/<sid>/session.jsonl``；本 Service 负责：
- SessionLog 的装配与懒加载（load + repair 合成关闭事件）；
- write-behind 持久化协调（WriteBehindCoordinator）；
- 事件 → session/event 帧转发（转发器由 plugin 注入 publisher）；
- 读侧派生（derive_messages / derive_context_messages + 缓存）；
- session.json 元信息 CRUD（委托 Repository，无消息）。

调用方不应直接读写 session.json / session.jsonl，也不应绕过本门面使用 repository。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ftre_agent.message import Msg
from ftre_agent.session import SessionLog, derive_context_messages, derive_messages
from ftre_agent.session.events import (
    AssistantMessageData,
)

from ftre.kernel.hooks import HookRuntime
from ftre.services.session.entity.models import (
    ExternalSessionModel,
    MessageModel,
    SessionModel,
    StatePageModel,
)
from ftre.services.session.entity.state import SessionMetaFile, SessionState
from ftre.services.session.hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from ftre.services.session.persistence.jsonl import (
    WriteBehindCoordinator,
    read_event_log,
    write_event_log_atomic,
)
from ftre.services.session.persistence.repository import (
    SessionRepository,
    summarize_last_user_text,
)

logger = logging.getLogger(__name__)

FramePublisher = Callable[..., Any]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class ForkResult:
    """创建分叉 Session 后返回的新身份和工作区信息。"""
    fork_session_id: str
    title: str
    workspace: str


class SessionService:
    """Session 持久化与事件日志的唯一门面。"""

    key = "sessions"

    def __init__(
        self,
        db_path: str | None = None,
        *,
        sessions_dir: str | None = None,
        hook_runtime: HookRuntime | None = None,
    ):
        self._repo = SessionRepository(db_path, sessions_dir=sessions_dir)
        self._hook_runtime = hook_runtime
        # ── 事件日志运行态 ──────────────────────────────
        self._logs: dict[str, SessionLog] = {}
        self._writer = WriteBehindCoordinator(self._repo.session_dir)
        # 帧发布器（plugin 注入；签名 publish_frame(session_id, channel_id, frame)）
        self._frame_publisher: FramePublisher | None = None
        self._forward_queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] | None = None
        self._forward_task: asyncio.Task | None = None
        # 读侧派生缓存：session_id → (last_seq, messages)
        self._derive_cache: dict[str, tuple[int, list[Msg]]] = {}
        # per-session 日志装配锁（懒加载并发首触去重）
        self._log_locks: dict[str, asyncio.Lock] = {}

    # ============================================================
    # 帧转发（plugin 装配）
    # ============================================================

    def set_frame_publisher(self, publisher: FramePublisher) -> None:
        """由 session plugin 注入 message_bus.publish_frame（唯一帧出口）。"""
        self._frame_publisher = publisher

    def _enqueue_forward(self, session_id: str, channel_id: str, event: dict) -> None:
        queue = self._forward_queue
        if queue is None:
            queue = asyncio.Queue()
            self._forward_queue = queue
            self._forward_task = asyncio.create_task(
                self._forward_loop(queue), name="session:frame-forwarder"
            )
        queue.put_nowait((session_id, channel_id, event))

    async def _forward_loop(self, queue: asyncio.Queue) -> None:
        """串行转发事件帧（顺序 = 事件序）；turn/end 追加 token_usage 投影帧。"""
        while True:
            session_id, channel_id, event = await queue.get()
            try:
                if self._frame_publisher is None:
                    continue
                from ftre.services.messaging.wire import SessionEventFrame

                frame = SessionEventFrame(
                    session_id=session_id, payload={"event": event}
                )
                await self._frame_publisher(session_id, channel_id, frame)
                if event.get("type") == "turn/end":
                    usage = (event.get("data") or {}).get("usage")
                    if isinstance(usage, dict) and usage:
                        from ftre.services.messaging.wire import SessionProjectionFrame

                        # turn/end.usage 是整轮累计统计；同时补发当前上下文
                        # 水位，客户端不必把累计 completion 当成 context 使用量。
                        projection_value = dict(usage)
                        try:
                            context_usage = await self.get_token_usage(session_id)
                            projection_value.update({
                                "context_tokens": context_usage.get("context_tokens", 0),
                                "pending_estimated": context_usage.get("pending_estimated", 0),
                            })
                        except Exception:
                            logger.debug(
                                "[session] token usage projection unavailable session=%s",
                                session_id,
                                exc_info=True,
                            )

                        await self._frame_publisher(
                            session_id,
                            channel_id,
                            SessionProjectionFrame(
                                session_id=session_id,
                                payload={
                                    "key": "token_usage",
                                    "value": projection_value,
                                    "seq": event.get("seq", 0),
                                },
                            ),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[session] 帧转发失败 session=%s seq=%s",
                    session_id, event.get("seq"),
                )

    # ============================================================
    # SessionLog 装配 / 懒加载
    # ============================================================

    def _channel_of(self, session_id: str) -> str:
        state = self._repo.get_state(session_id)
        return state.session.channel_id if state is not None else ""

    async def log(self, session_id: str) -> SessionLog:
        """取得（必要时懒加载 + repair）一个会话的事件日志。

        per-session 创建锁：并发首触时保证只有一个协程做 load/装配，
        其余等待并复用同一实例（避免内存日志实例被覆盖导致事件丢失）。
        """
        existing = self._logs.get(session_id)
        if existing is not None:
            return existing
        if self._repo.get_state(session_id) is None:
            raise ValueError(f"session 不存在: {session_id}")

        async with self._log_locks.setdefault(session_id, asyncio.Lock()):
            existing = self._logs.get(session_id)
            if existing is not None:
                return existing

            from ftre.services.session.repair import repair_events

            session_dir = self._repo.session_dir(session_id)
            stored = await asyncio.to_thread(read_event_log, session_dir)
            repaired = repair_events(stored)
            if len(repaired) != len(stored):
                # repair 不是临时投影：如果只把合成 turn/end 放在内存里，
                # 下一次重启仍会看到同一个未闭合 turn，既重复告警又可能重复收尾。
                # 在装配 SessionLog 前原子写回，随后 writer cursor 才能从真实磁盘
                # 前缀继续追加。
                await asyncio.to_thread(
                    write_event_log_atomic, session_dir, repaired
                )
            new_log = SessionLog(session_id)
            new_log.load(repaired)
            self._logs[session_id] = new_log
            # write-behind：已加载前缀（包括已原子写回的 repair）视为已持久化。
            self._writer.attach(session_id, new_log)
            # 帧转发
            channel_id = self._channel_of(session_id)
            new_log.subscribe(
                lambda event, sid=session_id, cid=channel_id: self._enqueue_forward(sid, cid, event)
            )
            if len(repaired) != len(stored):
                logger.info(
                    "[session] repair 注入 %d 个合成关闭事件 session=%s",
                    len(repaired) - len(stored), session_id,
                )
            return new_log

    async def append_event(
        self,
        session_id: str,
        type_: str,
        data: Any,
        *,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Runtime / Inbox / Compaction 的事件提交入口（同步语义，await 仅保证日志已装配）。"""
        log = await self.log(session_id)
        return log.append(type_, data, message_id=message_id)

    async def append_user_message_if_absent(
        self,
        session_id: str,
        *,
        request_id: str,
        content: list[Any],
        metadata: dict[str, Any] | None = None,
        previous_assistant_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """幂等提交用户消息事件（Inbox claim / AgentLoop 兜底共用）。

        返回 None 表示 request_id 已存在（幂等跳过）。同时维护反规范化
        last_user_text（会话列表预览）。``previous_assistant_message_id``
        保留参数位以兼容调用方；封口语义由 derive fold 的用户边界规则承担。
        """
        del previous_assistant_message_id
        log = await self.log(session_id)
        event = log.append_user_message(
            request_id=request_id, content=content, metadata=metadata
        )
        if event is None:
            return None
        preview = summarize_last_user_text([_user_msg_of_event(event)])
        if preview:
            await self._repo.set_last_user_text(session_id, preview)
        return event

    async def append_external_message(
        self,
        session_id: str,
        message: Msg,
    ) -> dict[str, Any]:
        """跨会话投递（ftre_messaging）：whole-value assistant/message 事件。"""
        return await self.append_event(
            session_id,
            "assistant/message",
            AssistantMessageData(message=message.model_dump(mode="json")),
            message_id=message.id,
        )

    async def flush_log(self, session_id: str) -> None:
        """语义 flush 屏障：强制把该会话积压事件写盘（幂等）。"""
        await self._writer.flush(session_id)

    async def get_events_page(
        self, session_id: str, *, after_seq: int, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """tail-page：seq > after_seq 的至多 limit 条事件（内存即权威）。"""
        log = await self.log(session_id)
        return log.tail(after_seq, limit)

    # ============================================================
    # 读侧派生（derive + 缓存）
    # ============================================================

    async def derived_messages(self, session_id: str) -> list[Msg]:
        """全量 transcript（含 hide 消息）；按 last_seq 缓存。"""
        log = await self.log(session_id)
        cached = self._derive_cache.get(session_id)
        if cached is not None and cached[0] == log.last_seq:
            return cached[1]
        messages = derive_messages(list(log.events))
        self._derive_cache[session_id] = (log.last_seq, messages)
        return messages

    async def _records(self, session_id: str) -> list[MessageModel]:
        messages = await self.derived_messages(session_id)
        return [self._repo.to_message_model(m, session_id) for m in messages]

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """按历史顺序读取 Session 的派生消息（HTTP /messages 数据源）。"""
        if self._repo.get_state(session_id) is None:
            return []
        return await self._records(session_id)

    async def get_messages_snapshot(
        self,
        session_id: str,
        *,
        limit_turns: int | None = None,
        before_ts: float | None = None,
    ) -> tuple[list[MessageModel], bool, int]:
        """返回同一事件快照派生的消息、分页标记和覆盖游标。

        事件列表和 ``last_seq`` 必须来自同一次内存读取。否则流式事件恰好
        在两次读取之间追加时，客户端会拿到旧消息和新游标，刷新后就会跳过
        尚未出现在消息列表里的 chunk。
        """
        if self._repo.get_state(session_id) is None:
            return [], False, -1

        log = await self.log(session_id)
        events = list(log.events)
        snapshot_seq = int(events[-1]["seq"]) if events else -1
        cached = self._derive_cache.get(session_id)
        if cached is not None and cached[0] == snapshot_seq:
            derived = cached[1]
        else:
            derived = derive_messages(events)
            self._derive_cache[session_id] = (snapshot_seq, derived)

        records = [self._repo.to_message_model(message, session_id) for message in derived]
        if limit_turns is None or limit_turns <= 0:
            return records, False, snapshot_seq
        page, has_more = self._paginate_records(
            records, limit_turns=limit_turns, before_ts=before_ts
        )
        return page, has_more, snapshot_seq

    async def last_seq(self, session_id: str) -> int:
        if self._repo.get_state(session_id) is None:
            return -1
        log = await self.log(session_id)
        return log.last_seq

    async def get_context_messages(self, session_id: str) -> list[MessageModel]:
        """返回给 LLM 使用的上下文消息：最后 summary compact 锚点 + tail。"""
        if self._repo.get_state(session_id) is None:
            return []
        log = await self.log(session_id)
        messages = derive_context_messages(list(log.events))
        return [self._repo.to_message_model(m, session_id) for m in messages]

    # ============================================================
    # lifecycle Hook
    # ============================================================

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
        """按关键字检索会话标题（元信息内存态扫描，线程池执行不阻塞）。"""
        from ftre.services.session.search import search_sessions

        snapshot = self._repo.all_states()
        return await asyncio.to_thread(search_sessions, snapshot, q, limit, workspace, offset)

    async def init(self) -> None:
        """启动：加载 session.json 元信息索引、清扫孤儿目录。

        事件日志懒加载（首次访问该会话时 load + repair），不在启动期全量读取。
        """
        await self._repo.init()
        await self._sweep_orphan_session_dirs()

    async def close(self) -> None:
        """安全幂等：drain write-behind、停帧转发；磁盘数据保留。"""
        if self._forward_task is not None:
            self._forward_task.cancel()
            await asyncio.gather(self._forward_task, return_exceptions=True)
            self._forward_task = None
            self._forward_queue = None
        await self._writer.close()
        self._logs.clear()
        self._derive_cache.clear()
        self._log_locks.clear()

    # ============================================================
    # Session CRUD（委托 storage）
    # ============================================================

    def create_id(self) -> str:
        """生成符合 Session 目录安全约束的新 ID。"""
        return self._repo.create_id()

    def session_dir(self, session_id: str) -> Path:
        """Session 目录（session.json + session.jsonl 所在目录）。"""
        return self._repo.session_dir(session_id)

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
        """同步只读幂等查询（仅元信息层；完整判定走 request_state）。"""
        del session_id, request_id
        return False

    async def request_state(
        self, session_id: str, request_id: str, run_id: str | None = None
    ) -> str | None:
        """查询请求是否已经产生过 Assistant 执行结果（turn/end 事件索引）。"""
        del run_id
        if self._repo.get_state(session_id) is None:
            return None
        log = await self.log(session_id)
        return log.request_state(request_id)

    async def update_session(
        self,
        session_id: str,
        title: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """更新标题/工作区等 Session 元信息。"""
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
        """删除 session（含事件日志目录）。

        若是 team leader：级联取消并删除全部成员 session 与 sub_agents profile 树。
        若是 team 成员（被单独删除）：反向从 leader 的 teams 摘除并删其 profile。
        """
        from ftre.services.agent_profile import (
            sub_agent as sub_agent_profile,  # 惰性导入避免包间循环
        )

        meta = await self.get_session_metadata(session_id)  # 不存在 → {}，幂等入口

        member_sids: list[str] = []
        teams = meta.get("teams")
        if isinstance(teams, dict):
            for team in teams.values():
                if isinstance(team, dict) and isinstance(team.get("members"), dict):
                    member_sids.extend(
                        k for k in team["members"] if isinstance(k, str)
                    )

        for msid in member_sids:
            self._writer.detach(msid)
            self._logs.pop(msid, None)
            self._derive_cache.pop(msid, None)
            self._log_locks.pop(msid, None)
            await self._repo.delete_session(msid)
            await self._emit_lifecycle("disposed", msid)

        sub_agent_profile.delete_all_profiles(self, session_id)

        self._writer.detach(session_id)
        self._logs.pop(session_id, None)
        self._derive_cache.pop(session_id, None)
        self._log_locks.pop(session_id, None)
        await self._repo.delete_session(session_id)
        await self._emit_lifecycle("disposed", session_id)

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
        """从 leader 的 metadata['teams'][team_id].members 摘除成员（原子 RMW）。"""

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
        """删除 sessions/ 下的孤儿目录：无 session.json 且非损坏隔离件。"""
        known_ids = {sid for sid, _ in self._repo.all_states()}
        try:
            root = self._repo.sessions_root()
            children = sorted(root.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name in known_ids:
                continue
            if (child / "session.json").exists() or (child / "session.jsonl").exists():
                continue  # 有正式文件却未加载 → 异常态，不动
            if list(child.glob("session.json.corrupt-*")):
                continue
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
    # 上下文裁剪（给 LLM 的上下文窗口与按轮分页）
    # ============================================================

    def build_user_content(
        self,
        content: Any,
        attachments: list[dict[str, Any]] | None,
        *,
        include_images: bool = True,
    ) -> str | list[dict[str, Any]]:
        """把用户输入与附件组装成 OpenAI 安全的 content（多模态 wire 归一）。"""
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
        """把派生 Msg 记录转换为 provider 消息列表。"""
        from ftre.services.session.message.converter import to_openai

        return to_openai(list(records), config={"llm": {"vision": vision}})

    def record_to_msg(self, record: MessageModel | Msg | dict[str, Any]) -> Msg:
        """把一条派生记录还原为 typed Msg（确认恢复路径使用）。"""
        from ftre.services.session.message.converter import _as_msg

        return _as_msg(record)

    async def get_recent_messages_by_turns(
        self, session_id: str, limit_turns: int = 5, before_ts: float | None = None
    ) -> tuple[list[MessageModel], bool]:
        """获取指定 session 最近 N 轮对话的所有消息（派生记录上分页）。"""
        records = await self._records(session_id)
        return self._paginate_records(
            records, limit_turns=limit_turns, before_ts=before_ts
        )

    @staticmethod
    def _paginate_records(
        records: list[MessageModel],
        *,
        limit_turns: int,
        before_ts: float | None,
    ) -> tuple[list[MessageModel], bool]:
        """在同一派生记录列表上按可见用户消息切最近轮次。"""
        if before_ts is not None:
            records = [r for r in records if r["timestamp"] < before_ts]
        if not records:
            return [], False

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
    # Token 用量
    # ============================================================

    async def get_token_usage(self, session_id: str) -> dict:
        """当前 token 用量（上下文口径；锚点 + 字符级粗估）。"""
        messages = await self.get_context_messages(session_id)
        return _compute_token_usage(session_id, messages)

    # ============================================================
    # 前端投影（派生分页只读视图，供 Inspector / HTTP API）
    # ============================================================

    async def get_state_page(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 50,
        max_string_chars: int = 20_000,
    ) -> StatePageModel | None:
        """派生消息 + 元信息的一致性分页快照（Inspector）。"""
        state = self._repo.get_state(session_id)
        if state is None:
            return None
        derived = await self.derived_messages(session_id)

        total = len(derived)
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
        for message in derived:
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
        for message in derived[page_offset:end]:
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
            "file_path": str(self._repo.session_dir(session_id) / "session.jsonl"),
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
        """按需读取派生消息中一条完整 Msg，供分页视图展开超大内容。"""
        derived = await self.derived_messages(session_id)
        for message in derived:
            if message.id == message_id:
                return message.model_dump(mode="json")
        return None

    # ============================================================
    # Fork：事件日志前缀拷贝
    # ============================================================

    FORK_METADATA_EXCLUDE = frozenset({"teams", "team_member", "external"})

    async def fork_session(self, parent_session_id: str) -> ForkResult:
        """把 parent 派生为独立 session：session.json 元信息 + 事件日志整份拷贝。

        说明：派生是 per-session 的，事件中的 message_id / request_id 无需
        重生成（不再存在跨 session Msg.id 全局索引）。
        """
        async with self._repo.lock_for(parent_session_id):
            parent_state = self._repo.get_state(parent_session_id)
            if parent_state is None:
                raise ValueError(f"session not found: {parent_session_id}")
            # 统一走 SessionLog 装配入口，确保未被访问过的父会话也先执行
            # torn-tail 截断和 open-turn repair，再复制事实日志。
            parent_log = await self.log(parent_session_id)
            events = list(parent_log.events)
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
            fork_metadata = {
                key: copy.deepcopy(value)
                for key, value in parent_state.metadata.items()
                if key not in self.FORK_METADATA_EXCLUDE
            }

        fork_metadata["forked_from"] = parent_session_id
        fork_metadata["forked_at"] = datetime.now(UTC).isoformat()
        new_state = SessionMetaFile(
            session=SessionState(
                id=fork_id,
                agent_id=parent_agent_id,
                channel_id=parent_channel_id,
                title=fork_title,
                workspace=fork_workspace,
                created_at=now,
                updated_at=now,
                last_user_text=parent_header.last_user_text,
            ),
            metadata=fork_metadata,
        )
        await self._repo.create_session_with_state(new_state)
        if events:
            await asyncio.to_thread(
                write_event_log_atomic, self._repo.session_dir(fork_id), events
            )
        return ForkResult(
            fork_session_id=fork_id,
            title=fork_title,
            workspace=fork_workspace,
        )


def _user_msg_of_event(event: dict[str, Any]) -> Msg:
    """user/message 事件 → Msg（供 last_user_text 摘要）。"""
    # 复用唯一读侧 fold，保证 skill/image 等开放 UI part 与 /messages
    # 使用同一归一规则；不要在预览路径再次直接构造严格 Msg。
    for message in derive_messages([event]):
        if message.role == "user":
            return message
    # 理论上 user/message 必然产生一条 user；保留显式异常便于定位损坏日志。
    raise ValueError("user/message 未能派生为 UserMsg")


def _truncate_large_strings(value: Any, *, max_chars: int) -> tuple[Any, bool]:
    """递归压缩超长字符串，避免派生分页被单个 base64/tool output 撑大。"""
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
    """根据派生 Msg 计算 token 用量（锚点 + 字符级粗估）。"""
    from .message.token_counter import estimate_messages_tokens

    anchor_index, last_call_usage = _find_last_call_usage(messages)
    pending_messages = messages[anchor_index + 1:] if anchor_index >= 0 else messages
    pending_estimated = estimate_messages_tokens(pending_messages)

    if last_call_usage is None:
        return {
            "session_id": session_id,
            "last_call_usage": None,
            "pending_estimated": pending_estimated,
            "context_tokens": pending_estimated,
            "total": pending_estimated,
        }

    prompt_tokens = int(last_call_usage.get("prompt_tokens") or 0)
    completion_tokens = int(last_call_usage.get("completion_tokens") or 0)
    total_tokens = int(last_call_usage.get("total_tokens") or 0)
    # context_tokens 是下一次请求的上下文水位；total 继续保留为公开统计值，
    # 避免把 completion 计入上下文判断，也不破坏现有 token_usage API。
    # 某些兼容网关只返回 total_tokens。此时不能把 completion 也当成
    # 下一次 prompt 的上下文基数，否则一次长回复会立即伪造“上下文超限”。
    prompt_base = (
        prompt_tokens
        if prompt_tokens > 0
        else max(0, total_tokens - completion_tokens)
    )
    context_tokens = prompt_base + pending_estimated

    return {
        "session_id": session_id,
        "last_call_usage": {
            "prompt_tokens": int(last_call_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(last_call_usage.get("completion_tokens") or 0),
            "total_tokens": int(last_call_usage.get("total_tokens") or 0),
        },
        "pending_estimated": pending_estimated,
        "context_tokens": context_tokens,
        "total": total_tokens + pending_estimated,
    }
