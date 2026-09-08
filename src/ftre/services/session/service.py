"""SessionService —— Session 元信息、Msg 快照和 live Event 的唯一门面。

SessionLog 只负责当前进程的事件顺序与直播；SnapshotCoordinator 在定时或语义
边界把完整 Msg 列表原子写入同一个 ``session.json``。调用方不应直接读写磁盘。
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

from ftre_agent.message import (
    Msg,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)
from ftre_agent.session import SessionLog, derive_messages, request_fingerprint
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
from ftre.services.session.entity.state import (
    CURRENT_SCHEMA_VERSION,
    SessionMetaFile,
    SessionState,
)
from ftre.services.session.hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from ftre.services.session.persistence.repository import (
    SessionRepository,
    summarize_last_user_text,
)
from ftre.services.session.persistence.snapshot import (
    SnapshotCoordinator,
    legacy_log_path,
    read_legacy_events,
    repair_legacy_events,
)

logger = logging.getLogger(__name__)

FramePublisher = Callable[..., Any]
ContextViewBuilder = Callable[[list[Msg]], list[Msg]]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class ForkResult:
    """创建分叉 Session 后返回的新身份和工作区信息。"""
    fork_session_id: str
    title: str
    workspace: str
    parent_session_id: str = ""
    through_message_id: str | None = None
    seq: int = -1


class ForkBusyError(RuntimeError):
    """父 Session 在运行态，不能生成一致的分支快照。"""


class ForkTargetError(ValueError):
    """Fork 截止点不是可用的完整 Msg。"""


@dataclass
class RollbackResult:
    """原地回滚当前 Session 后返回的边界和输入框回填内容。"""

    session_id: str
    through_message_id: str
    seq: int
    removed_message_ids: list[str]
    prefill_content: list[dict[str, Any]]
    title: str
    workspace: str


class RollbackBusyError(RuntimeError):
    """当前 Session 仍有运行态或 checkpoint 竞争，不能原地回滚。"""


class RollbackTargetError(ValueError):
    """回滚目标不是稳定的 user Msg。"""


class SessionService:
    """Session 持久化与事件日志的唯一门面。"""

    key = "sessions"

    def __init__(
        self,
        db_path: str | None = None,
        *,
        sessions_dir: str | None = None,
        hook_runtime: HookRuntime | None = None,
        snapshot_interval_ms: int = 500,
    ):
        self._repo = SessionRepository(db_path, sessions_dir=sessions_dir)
        self._hook_runtime = hook_runtime
        # ── 运行态：live Event 与已落盘 Msg 基线 ─────────────
        self._logs: dict[str, SessionLog] = {}
        self._baseline_messages: dict[str, list[Msg]] = {}
        self._snapshot = SnapshotCoordinator(
            self._write_snapshot,
            interval_ms=snapshot_interval_ms,
        )
        # 帧发布器（plugin 注入；签名 publish_frame(session_id, channel_id, frame)）
        self._frame_publisher: FramePublisher | None = None
        self._forward_queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] | None = None
        self._forward_task: asyncio.Task | None = None
        # 读侧派生缓存：session_id → (live_seq, merged messages)
        self._derive_cache: dict[str, tuple[int, list[Msg]]] = {}
        # per-session 日志装配锁（懒加载并发首触去重）
        self._log_locks: dict[str, asyncio.Lock] = {}
        # 可选 ContextView 投影器由业务 Plugin 注入；SessionService 本身只
        # 保存完整 Msg，未安装压缩包时默认使用完整历史。
        self._context_view_builder: ContextViewBuilder | None = None

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
        """串行转发 live Event（顺序 = 当前进程事件序）；turn/end 追加 usage。"""
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

    def _on_event(self, session_id: str, channel_id: str, event: dict[str, Any]) -> None:
        """Event 提交后的唯一运行态 fan-out：直播、缓存失效和 checkpoint。"""
        self._derive_cache.pop(session_id, None)
        self._enqueue_forward(session_id, channel_id, event)
        event_type = event.get("type")
        self._snapshot.mark_dirty(
            session_id,
            immediate=event_type in {
                "user/message",
                "assistant/message",
                "tool/result",
                "compact/message",
                "hint/message",
                "approval/asked",
                "turn/end",
            },
        )

    # ============================================================
    # SessionLog 装配 / 懒加载
    # ============================================================

    def _channel_of(self, session_id: str) -> str:
        state = self._repo.get_state(session_id)
        return state.session.channel_id if state is not None else ""

    async def log(self, session_id: str) -> SessionLog:
        """取得一个会话的 live EventLog，并加载已落盘的 Msg Snapshot。

        旧 F43 JSONL 只在这里做一次导入：先 derive 成 Msg、原子写入 schema v5，
        成功后删除旧文件；新运行永远不会从磁盘恢复 chunk Event。
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

            session_dir = self._repo.session_dir(session_id)
            state = self._repo.get_state(session_id)
            if state is None:
                raise ValueError(f"session 不存在: {session_id}")

            baseline = self._load_snapshot_messages(state)
            legacy_path = legacy_log_path(session_dir)
            migrated = False
            if not baseline and legacy_path.exists():
                stored = await asyncio.to_thread(read_legacy_events, session_dir)
                repaired = repair_legacy_events(stored)
                baseline = derive_messages(repaired)
                migrated = True
                request_index = self._request_index_from_events(repaired)
                migrated_state = state.model_copy(
                    deep=True,
                    update={
                        "schema_version": CURRENT_SCHEMA_VERSION,
                        "seq": int(repaired[-1]["seq"]) if repaired else state.seq,
                        "messages": [m.model_dump(mode="json") for m in baseline],
                        "requests": request_index,
                    },
                )
                preview = summarize_last_user_text(baseline)
                if preview:
                    migrated_state.session.last_user_text = preview
                await self._repo.commit(migrated_state)
                state = migrated_state
                try:
                    await asyncio.to_thread(legacy_path.unlink, True)
                except (FileNotFoundError, OSError):
                    logger.warning(
                        "[session-snapshot] legacy JSONL cleanup deferred path=%s",
                        legacy_path,
                    )

            self._baseline_messages[session_id] = baseline
            new_log = SessionLog(session_id, start_seq=int(state.seq) + 1)
            self._logs[session_id] = new_log
            channel_id = self._channel_of(session_id)
            new_log.subscribe(
                lambda event, sid=session_id, cid=channel_id: self._on_event(sid, cid, event)
            )
            if migrated:
                logger.info(
                    "[session-snapshot] migrated legacy session=%s messages=%d",
                    session_id,
                    len(baseline),
                )
            return new_log

    @staticmethod
    def _load_snapshot_messages(state: SessionMetaFile) -> list[Msg]:
        """从 schema v5 文件解析完整 Msg；坏的一条不应吞掉整份会话。"""
        messages: list[Msg] = []
        for index, raw in enumerate(state.messages):
            try:
                messages.append(Msg.model_validate(raw))
            except Exception as exc:
                raise ValueError(
                    f"session snapshot message[{index}] 无法解析: {exc}"
                ) from exc
        return messages

    @staticmethod
    def _request_index_from_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """从旧 Event 构造最小 request 幂等索引。"""
        index: dict[str, dict[str, Any]] = {}
        for event in events:
            type_ = event.get("type")
            data = event.get("data") or {}
            if type_ == "user/message":
                request_id = str(data.get("request_id") or "")
                if request_id:
                    index.setdefault(
                        request_id,
                        {
                            "message_id": event.get("message_id") or "",
                            "run_id": "",
                            "status": "pending",
                            "fingerprint": request_fingerprint(data.get("content")),
                        },
                    )
            elif type_ == "turn/start":
                request_id = str(data.get("request_id") or "")
                if request_id:
                    record = index.setdefault(request_id, {})
                    record.update({"run_id": data.get("turn_id") or "", "status": "running"})
            elif type_ == "turn/end":
                request_id = str(data.get("request_id") or "")
                if request_id:
                    record = index.setdefault(request_id, {})
                    outcome = str(data.get("outcome") or "error")
                    record["status"] = "completed" if outcome == "completed" else "failed"
        return index

    @staticmethod
    def _merge_messages(baseline: list[Msg], live: list[Msg]) -> list[Msg]:
        """将 checkpoint 基线与本进程未落盘的 live fold 合并，按消息 id 替换。"""
        result = [message.model_copy(deep=True) for message in baseline]
        positions = {message.id: index for index, message in enumerate(result)}
        for message in live:
            copy_message = message.model_copy(deep=True)
            index = positions.get(copy_message.id)
            if index is None:
                positions[copy_message.id] = len(result)
                result.append(copy_message)
            else:
                result[index] = copy_message
        return result

    async def _write_snapshot(self, session_id: str) -> None:
        """生成并原子提交一次完整 Msg Snapshot。"""
        state = self._repo.get_state(session_id)
        if state is None:
            return
        log = self._logs.get(session_id)
        if log is None:
            return
        # 在任何 await 之前捕获同一份事件视图和水位。否则事件在 derive 与
        # commit 之间追加时，快照可能写入旧 Msg 却声明了新的 seq，客户端
        # attach 会因此跳过尚未出现在 Msg 中的事件。
        events = list(log.events)
        snapshot_seq = log.seq
        baseline = self._baseline_messages.get(session_id, [])
        messages = self._merge_messages(baseline, derive_messages(events))
        event_requests = self._request_index_from_events(events)
        for message in messages:
            request_id = str(message.metadata.get("request_id") or "")
            if request_id:
                record = event_requests.setdefault(
                    request_id,
                    {"message_id": message.id, "run_id": "", "status": "pending"},
                )
                record.setdefault("message_id", message.id)
                record.setdefault(
                    "fingerprint",
                    request_fingerprint(
                        [block.model_dump(mode="json") for block in message.content]
                    ),
                )
        async with self._repo.lock_for(session_id):
            current = self._repo.get_state(session_id)
            if current is None:
                return
            # 另一个快照已经覆盖了更高水位时，本次旧快照不能回写覆盖它。
            if int(current.seq) > snapshot_seq:
                return
            requests = dict(current.requests)
            requests.update(event_requests)
            new_state = current.model_copy(
                deep=True,
                update={
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "seq": snapshot_seq,
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "requests": requests,
                },
            )
            new_state.session.updated_at = _now_iso()
            preview = summarize_last_user_text(messages)
            if preview:
                new_state.session.last_user_text = preview
            await self._repo.commit(new_state)

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
        self._validate_persisted_request(session_id, request_id, content)
        if self._has_persisted_request(session_id, request_id):
            # Snapshot 已经记录过该 request；Inbox 仍可继续 claim pending 项，
            # 但绝不能再追加第二个 user/message 气泡。
            return None
        log = await self.log(session_id)
        # legacy JSONL 可能在上面的检查后才完成迁移；再次检查避免重启时
        # 同一个 request 生成第二条用户消息。
        self._validate_persisted_request(session_id, request_id, content)
        if self._has_persisted_request(session_id, request_id):
            return None
        event = log.append_user_message(
            request_id=request_id, content=content, metadata=metadata
        )
        if event is None:
            return None
        preview = summarize_last_user_text([_user_msg_of_event(event)])
        if preview:
            await self._repo.set_last_user_text(session_id, preview)
        return event

    def _has_persisted_request(self, session_id: str, request_id: str) -> bool:
        if not request_id:
            return False
        state = self._repo.get_state(session_id)
        if state is not None and request_id in state.requests:
            return True
        return any(
            str(message.metadata.get("request_id") or "") == request_id
            for message in self._baseline_messages.get(session_id, [])
        )

    def _validate_persisted_request(
        self, session_id: str, request_id: str, content: list[Any]
    ) -> None:
        """跨重启复用 request_id 时拒绝绑定到不同内容。"""
        if not request_id:
            return
        expected = request_fingerprint(content)
        state = self._repo.get_state(session_id)
        record = state.requests.get(request_id) if state is not None else None
        if isinstance(record, dict):
            stored = record.get("fingerprint")
            if isinstance(stored, str) and stored and stored != expected:
                raise ValueError(f"request_id 已绑定不同内容: {request_id}")
            if isinstance(stored, str) and stored:
                return
        for message in self._baseline_messages.get(session_id, []):
            if str(message.metadata.get("request_id") or "") != request_id:
                continue
            actual = request_fingerprint(
                [block.model_dump(mode="json") for block in message.content]
            )
            if actual != expected:
                raise ValueError(f"request_id 已绑定不同内容: {request_id}")
            return

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
        """语义 flush 屏障：强制把该会话的 Msg Snapshot 写盘。"""
        if session_id in self._logs:
            self._snapshot.mark_dirty(session_id, immediate=True)
        await self._snapshot.flush(session_id)

    async def events_after(
        self, session_id: str, *, after_seq: int, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool, bool, int]:
        """返回基线之后尚未折叠为 Msg 的事件。

        ``resync_required`` 表示客户端水位早于磁盘快照或领先当前 Session，
        这时 attach 不能凭空补造旧事件，调用方应重新请求 HTTP /messages。
        """
        state = self._repo.get_state(session_id)
        if state is None:
            return [], False, True, -1
        log = await self.log(session_id)
        current_seq = log.seq
        persisted_seq = int(state.seq)
        if after_seq < persisted_seq or after_seq > current_seq:
            return [], False, True, current_seq
        events, has_more = log.tail(after_seq, limit)
        return events, has_more, False, current_seq

    # ============================================================
    # 读侧派生（derive + 缓存）
    # ============================================================

    async def derived_messages(self, session_id: str) -> list[Msg]:
        """全量 transcript（含 hide 消息）：checkpoint 基线 + live Event fold。"""
        log = await self.log(session_id)
        cached = self._derive_cache.get(session_id)
        if cached is not None and cached[0] == log.seq:
            return cached[1]
        baseline = self._baseline_messages.get(session_id, [])
        messages = self._merge_messages(baseline, derive_messages(list(log.events)))
        self._derive_cache[session_id] = (log.seq, messages)
        return messages

    async def get_full_messages(self, session_id: str) -> list[Msg]:
        """返回完整的 Provider 无关 Msg 快照，供 Agent Runtime 构建 ContextView。

        这里故意不应用 compact/fast 裁剪。上下文策略由 Agent Hook（例如
        ``ftre-compaction`` 的 ``agent/context-build``）在本次请求的深拷贝上
        决定；SessionService 只提供完整历史，避免把某个可选 Plugin 的策略
        固化进 Session Owner。
        """
        if self._repo.get_state(session_id) is None:
            return []
        return [
            message.model_copy(deep=True)
            for message in await self.derived_messages(session_id)
        ]

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
        """返回同一事件快照派生的消息、分页标记和覆盖 seq。

        事件列表和 ``seq`` 必须来自同一次内存读取。否则流式事件恰好
        在两次读取之间追加时，客户端会拿到旧消息和新游标，刷新后就会跳过
        尚未出现在消息列表里的 chunk。
        """
        if self._repo.get_state(session_id) is None:
            return [], False, -1

        log = await self.log(session_id)
        events = list(log.events)
        snapshot_seq = log.seq
        cached = self._derive_cache.get(session_id)
        if cached is not None and cached[0] == snapshot_seq:
            derived = cached[1]
        else:
            baseline = self._baseline_messages.get(session_id, [])
            derived = self._merge_messages(baseline, derive_messages(events))
            self._derive_cache[session_id] = (snapshot_seq, derived)

        records = [self._repo.to_message_model(message, session_id) for message in derived]
        if limit_turns is None or limit_turns <= 0:
            return records, False, snapshot_seq
        page, has_more = self._paginate_records(
            records, limit_turns=limit_turns, before_ts=before_ts
        )
        return page, has_more, snapshot_seq

    async def seq(self, session_id: str) -> int:
        if self._repo.get_state(session_id) is None:
            return -1
        log = await self.log(session_id)
        return log.seq

    async def get_context_messages(self, session_id: str) -> list[MessageModel]:
        """返回由可选 ContextView 投影器生成的读侧消息视图。

        该方法只服务于 token 统计等 Host 读接口，不参与 Agent Runtime 的
        LLM 调用。默认返回完整历史；压缩 Plugin 可注入一个纯函数，使统计
        口径与 ``agent/context-build`` 保持一致。SessionService 不解释任何
        compact/fast marker。
        """
        if self._repo.get_state(session_id) is None:
            return []
        messages = await self.get_full_messages(session_id)
        builder = self._context_view_builder
        if builder is not None:
            projected = builder([message.model_copy(deep=True) for message in messages])
            if not isinstance(projected, (list, tuple)):
                raise TypeError("ContextView builder must return a message sequence")
            messages = [
                message.model_copy(deep=True)
                if isinstance(message, Msg)
                else Msg.model_validate(message)
                for message in projected
            ]
        return [self._repo.to_message_model(m, session_id) for m in messages]

    def set_context_view_builder(
        self, builder: ContextViewBuilder | None
    ) -> Callable[[], bool]:
        """安装一个可逆的通用 ContextView 投影器。

        业务规则由 Plugin 持有；返回的 disposer 只恢复上一个投影器，供
        Plugin Effect 在卸载时调用。重复调用 disposer 安全无副作用。
        """
        if builder is not None and not callable(builder):
            raise TypeError("ContextView builder must be callable or None")
        previous = self._context_view_builder
        self._context_view_builder = builder
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            if self._context_view_builder is builder:
                self._context_view_builder = previous
            return True

        return dispose

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
        """启动：加载 session.json Snapshot 索引并清扫孤儿目录。

        Msg Snapshot 与旧 JSONL 迁移在首次访问该会话时懒加载，避免启动阶段读取
        所有大历史文件。
        """
        await self._repo.init()
        await self._sweep_orphan_session_dirs()

    async def close(self) -> None:
        """安全幂等：drain Snapshot、停帧转发；磁盘数据保留。"""
        if self._forward_task is not None:
            self._forward_task.cancel()
            await asyncio.gather(self._forward_task, return_exceptions=True)
            self._forward_task = None
            self._forward_queue = None
        await self._snapshot.close()
        self._logs.clear()
        self._baseline_messages.clear()
        self._derive_cache.clear()
        self._log_locks.clear()
        self._context_view_builder = None

    # ============================================================
    # Session CRUD（委托 storage）
    # ============================================================

    def create_id(self) -> str:
        """生成符合 Session 目录安全约束的新 ID。"""
        return self._repo.create_id()

    def session_dir(self, session_id: str) -> Path:
        """Session 目录（唯一持久化文件为 session.json）。"""
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
        """同步只读幂等查询（完整状态由 request_state 异步读取）。"""
        state = self._repo.get_state(session_id)
        return bool(state and request_id and request_id in state.requests)

    async def request_state(
        self, session_id: str, request_id: str, run_id: str | None = None
    ) -> str | None:
        """查询请求是否已经产生过可阻止重复执行的状态。"""
        del run_id
        if self._repo.get_state(session_id) is None:
            return None
        state = self._repo.get_state(session_id)
        if state is not None:
            record = state.requests.get(request_id)
            if isinstance(record, dict):
                status = str(record.get("status") or "")
                if status in {"completed", "failed", "running"}:
                    return "completed" if status == "completed" else status
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
            self._snapshot.detach(msid)
            self._logs.pop(msid, None)
            self._baseline_messages.pop(msid, None)
            self._derive_cache.pop(msid, None)
            self._log_locks.pop(msid, None)
            await self._repo.delete_session(msid)
            await self._emit_lifecycle("disposed", msid)

        sub_agent_profile.delete_all_profiles(self, session_id)

        self._snapshot.detach(session_id)
        self._logs.pop(session_id, None)
        self._baseline_messages.pop(session_id, None)
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
    # 消息转换与 token 读侧兼容入口
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
            "file_path": str(self._repo.session_dir(session_id) / "session.json"),
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
    # Fork：Msg Snapshot 深拷贝
    # ============================================================

    FORK_METADATA_EXCLUDE = frozenset({"teams", "team_member", "external"})

    async def fork_session(
        self,
        parent_session_id: str,
        *,
        through_message_id: str | None = None,
    ) -> ForkResult:
        """从完整 Snapshot 创建独立分支，不复制 ContextView 或运行态。

        ``through_message_id`` 是 Msg 边界，不是 seq；截止消息包含在子会话中。
        回滚是另一条原地修改当前 Session 的操作，由 ``rollback_session`` 负责。
        调用方应在进入本方法前拒绝 active Turn；这里仍会做 checkpoint 水位复核，
        避免复制未落盘的半成品。
        """
        # 先把当前进程中的 live Event 收敛到 Snapshot；随后在 Repo 锁内
        # 再核对 seq，若仍有事件在飞行，宁可返回 busy 也不复制旧基线。
        try:
            await self.flush_log(parent_session_id)
        except Exception as exc:
            raise ForkBusyError(f"session checkpoint 未完成: {parent_session_id}") from exc

        async with self._repo.lock_for(parent_session_id):
            parent_state = self._repo.get_state(parent_session_id)
            if parent_state is None:
                raise ValueError(f"session not found: {parent_session_id}")
            parent_log = self._logs.get(parent_session_id)
            if parent_log is not None and int(parent_state.seq) < int(parent_log.seq):
                raise ForkBusyError(f"session 仍有未完成 checkpoint: {parent_session_id}")
            messages = [
                message.model_copy(deep=True)
                for message in await self.derived_messages(parent_session_id)
            ]
            parent_header = parent_state.session

            target_index: int | None = None
            target: Msg | None = None
            if through_message_id:
                for index, message in enumerate(messages):
                    if message.id == through_message_id:
                        target_index = index
                        target = message
                        break
                if target_index is None or target is None:
                    raise ForkTargetError(
                        f"through_message_id 不存在: {through_message_id}"
                    )
                if not self._is_stable_message(target):
                    raise ForkTargetError(
                        f"目标消息尚未完成，不能 Fork: {through_message_id}"
                    )
            if target_index is None:
                selected_messages = messages
            else:
                selected_messages = messages[: target_index + 1]

            # 只允许稳定消息进入新 Snapshot；运行中的 tool result / approval
            # 不应被复制成一个看似可继续的历史。
            if any(not self._is_stable_message(message) for message in selected_messages):
                raise ForkBusyError(f"session 含未完成消息，不能 Fork: {parent_session_id}")

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
            fork_metadata.update(
                {
                    "forked_from": parent_session_id,
                    "forked_at": datetime.now(UTC).isoformat(),
                    "forked_through_message_id": through_message_id or "",
                }
            )
            fork_seq = max(
                (int(message.seq) for message in selected_messages if int(message.seq) >= 0),
                default=-1,
            )
            last_user_text = summarize_last_user_text(selected_messages) or ""
            new_state = SessionMetaFile(
                session=SessionState(
                    id=fork_id,
                    agent_id=parent_agent_id,
                    channel_id=parent_channel_id,
                    title=fork_title,
                    workspace=fork_workspace,
                    created_at=now,
                    updated_at=now,
                    last_user_text=last_user_text,
                ),
                metadata=fork_metadata,
                seq=fork_seq,
                messages=[message.model_dump(mode="json") for message in selected_messages],
                # request_id/run_id 只对父 Session 有意义，子 Session 从空索引开始。
                requests={},
                extensions=copy.deepcopy(parent_state.extensions),
            )

        await self._repo.create_session_with_state(new_state)
        return ForkResult(
            fork_session_id=fork_id,
            title=fork_title,
            workspace=fork_workspace,
            parent_session_id=parent_session_id,
            through_message_id=through_message_id,
            seq=fork_seq,
        )

    async def rollback_session(
        self,
        session_id: str,
        *,
        through_message_id: str,
    ) -> RollbackResult:
        """原地回滚当前 Session，并返回被移除用户消息的回填内容。

        ``through_message_id`` 必须指向稳定的 user Msg。该消息及其之后的
        历史从当前 Snapshot 中移除，父 Session 身份、metadata 和 workspace
        保持不变；不会创建子 Session，也不会重新执行任何副作用。
        """
        if not isinstance(through_message_id, str) or not through_message_id:
            raise RollbackTargetError("rollback 必须提供 through_message_id")

        try:
            await self.flush_log(session_id)
        except Exception as exc:
            raise RollbackBusyError(f"session checkpoint 未完成: {session_id}") from exc

        async with self._repo.lock_for(session_id):
            state = self._repo.get_state(session_id)
            if state is None:
                raise ValueError(f"session not found: {session_id}")
            log = self._logs.get(session_id)
            if log is not None and int(state.seq) < int(log.seq):
                raise RollbackBusyError(f"session 仍有未完成 checkpoint: {session_id}")

            messages = [
                message.model_copy(deep=True)
                for message in await self.derived_messages(session_id)
            ]
            target_index: int | None = None
            target: Msg | None = None
            for index, message in enumerate(messages):
                if message.id == through_message_id:
                    target_index = index
                    target = message
                    break
            if target_index is None or target is None:
                raise RollbackTargetError(
                    f"through_message_id 不存在: {through_message_id}"
                )
            if target.role != "user":
                raise RollbackTargetError("rollback 的截止点必须是 user Msg")
            if not self._is_stable_message(target):
                raise RollbackTargetError(
                    f"目标消息尚未完成，不能 rollback: {through_message_id}"
                )

            kept_messages = messages[:target_index]
            if any(not self._is_stable_message(message) for message in kept_messages):
                raise RollbackBusyError(f"session 含未完成消息，不能 rollback: {session_id}")

            kept_ids = {message.id for message in kept_messages}
            kept_request_ids = {
                str(message.metadata.get("request_id") or "")
                for message in kept_messages
                if str(message.metadata.get("request_id") or "")
            }
            requests = {
                request_id: copy.deepcopy(record)
                for request_id, record in state.requests.items()
                if request_id in kept_request_ids
                or (
                    isinstance(record, dict)
                    and str(record.get("message_id") or "") in kept_ids
                )
            }
            snapshot_seq = int(state.seq)
            new_state = state.model_copy(
                deep=True,
                update={
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "seq": snapshot_seq,
                    "messages": [
                        message.model_dump(mode="json") for message in kept_messages
                    ],
                    "requests": requests,
                },
            )
            new_state.session.updated_at = _now_iso()
            new_state.session.last_user_text = summarize_last_user_text(kept_messages) or ""
            await self._repo.commit(new_state)

            # Snapshot 已经落盘成功，才切换当前进程的完整历史基线；保留
            # SessionLog 的订阅者，但丢弃被回滚的 live tail。
            self._baseline_messages[session_id] = [
                message.model_copy(deep=True) for message in kept_messages
            ]
            if log is not None:
                log.reset(start_seq=snapshot_seq + 1)
            self._derive_cache.pop(session_id, None)

            prefill_content = [
                block.model_dump(mode="json") for block in target.content
            ]
            return RollbackResult(
                session_id=session_id,
                through_message_id=through_message_id,
                seq=snapshot_seq,
                removed_message_ids=[message.id for message in messages[target_index:]],
                prefill_content=prefill_content,
                title=new_state.session.title,
                workspace=new_state.session.workspace,
            )

    @staticmethod
    def _is_stable_message(message: Msg) -> bool:
        """检查 Msg 是否包含未闭合的工具状态。"""
        for block in message.content:
            if isinstance(block, ToolResultBlock) and block.state == ToolResultState.RUNNING:
                return False
            if isinstance(block, ToolCallBlock) and block.state != ToolCallState.FINISHED:
                return False
        return True


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
