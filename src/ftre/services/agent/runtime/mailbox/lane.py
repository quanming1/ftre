"""AgentLoop 内部的 SessionLane。

一个 Lane 对应一个 session，是“同 session 串行”的唯一所有者：它从 MailboxStore
领取队首请求、穿过 ContextGate、交给 TurnExecutor，再领取下一条。
Channel、Bus、HTTP 和工具都不能绕过它直接运行 Turn。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ftre.services.messaging.bus import BusMessage, InboundMetadata
from ftre.services.session.entity.state import MailboxState, QueueItem
from ftre.services.session.service import RequestAdmission

from ..loop.completion_registry import CompletionRegistry
from ..loop.context_gate import ContextGate
from ..loop.turn_executor import TurnExecutor, TurnOutcome
from .store import MailboxStore

logger = logging.getLogger(__name__)

SnapshotPublisher = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class AdmissionResult:
    """Bus request/reply 返回给入口的耐久接纳凭据。"""

    accepted: bool  # 是否成功接纳（入队或立即执行）；False 时 error 必填
    session_id: str  # 目标 session；SessionLaneRegistry 兜底回填后始终有效
    request_id: str = ""  # 持久化请求唯一 ID，ACK/取消/同步等待都引用它；未接纳时为空
    queue_position: int = 0  # pending 中的 1-based 排队位置；0 表示未入队或已被领取
    created: bool = False  # False 表示该 request_id 已在 pending/UserMessage 存在（重复提交或重放）
    error: dict | None = None  # 拒绝原因 {"code", "message", "retryable"}；accepted=True 时恒为 None


@dataclass
class TurnOperation:
    """唯一可取消的活跃 Turn；只存在内存，Gateway 重启后不自动重放。"""

    item: QueueItem
    turn_id: str
    task: asyncio.Task | None
    # 运行态不写入 state.json。一个字段足以说明当前 Turn 是执行、取消还是手工压缩。
    state: Literal["running", "cancelling", "compacting"] = "running"


@dataclass
class CompactOperation:
    """Lane 正在等待的 ContextGate；与 TurnOperation 互斥。"""

    reason: str


@dataclass
class BlockedOperation:
    """压缩失败且无法安全继续时保留队首，不丢消息也不盲目重试。"""

    reason: str


class SessionLane:
    """一个 session 的单 actor。它只推进 pending，运行状态只留在内存。"""

    def __init__(
        self,
        session_id: str,
        *,
        mailbox: MailboxStore,
        context_gate: ContextGate,
        executor: TurnExecutor,
        completion: CompletionRegistry,
        publish_snapshot: SnapshotPublisher,
    ) -> None:
        self.session_id = session_id
        self._mailbox = mailbox
        self._context_gate = context_gate
        self._executor = executor
        self._completion = completion
        self._publish_snapshot = publish_snapshot
        self._worker: asyncio.Task | None = None
        self._operation: TurnOperation | CompactOperation | BlockedOperation | None = None
        self._closed = False
        # admission_lock 同时保护 submit/close：close 一旦获锁后，不会再接纳新请求。
        self._admission_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def operation(self) -> TurnOperation | CompactOperation | BlockedOperation | None:
        return self._operation

    async def submit(self, inbound: BusMessage) -> AdmissionResult:
        """durable admission 的唯一入口：先落盘，后启动 worker，最后回复 ACK。"""
        # admission_lock 同时保护 submit/close：一旦 close 设置 _closed，后续请求只能拒绝，
        # 不会出现“返回 accepted 后 session 已经被删除”的竞态。
        async with self._admission_lock:
            if self._closed:
                return AdmissionResult(
                    accepted=False,
                    session_id=self.session_id,
                    error={"code": "session_closing", "message": "会话正在关闭", "retryable": False},
                )
            try:
                admission = await self._mailbox.admit(inbound)
            except OverflowError as exc:
                return AdmissionResult(
                    accepted=False,
                    session_id=self.session_id,
                    error={"code": "queue_full", "message": str(exc), "retryable": True},
                )
            except (ValueError, KeyError) as exc:
                return AdmissionResult(
                    accepted=False,
                    session_id=self.session_id,
                    error={"code": "admission_rejected", "message": str(exc), "retryable": False},
                )
            result = self._admission_result(admission)
            # 先发送“已经进入 pending”的快照，再启动 worker。浏览器先看到横幅，
            # 随后由 UserMessage 事件将该项自然转入聊天历史。
            await self._publish_snapshot(self.session_id)
            self._ensure_worker()
            return result

    @staticmethod
    def _admission_result(admission: RequestAdmission) -> AdmissionResult:
        return AdmissionResult(
            accepted=True,
            session_id=admission.session_id,
            request_id=admission.request_id,
            queue_position=admission.queue_position,
            created=admission.created,
        )

    def _ensure_worker(self) -> None:
        """同一时刻至多一个 drain task；它是 Lane 的串行保证。"""
        if self._closed or isinstance(self._operation, BlockedOperation):
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(), name=f"session-lane:{self.session_id}"
            )

    async def _drain(self) -> None:
        """严格顺序：pending → ContextGate → 内存 Turn → 下一条。"""
        try:
            while not self._closed:
                # 这里只查看队首，不立即 claim：队首仍属于 pending，
                # 因而在压缩或取消期间不会提前进入 LLM 上下文。
                item = await self._mailbox.peek(self.session_id)
                if item is None:
                    return

                # 每条消息可能来自不同 agent 配置；必须先解析本条消息的上下文窗口，
                # 再用对应的 80% 强制水位做领取前检查。
                channel_id = await self._mailbox.channel_id(self.session_id)
                inbound = self._to_inbound(item, channel_id)
                config, profile = await self._executor.resolve_inbound_config(
                    inbound, turn_id=f"config_{item.request_id}"
                )
                before = await self._context_gate.before_claim(
                    self.session_id, channel_id, config, item
                )
                if before.action == "compact":
                    # 压缩完成前不领取队首，因此后续 pending 消息也不会被压缩遗漏。
                    if not await self._compact_or_block(channel_id, config, before.reason):
                        return
                    # compact 后上下文已变化，重新对同一队首做安全判断。
                    continue

                taken = await self._mailbox.take(self.session_id, item.request_id)
                if taken is None:
                    # 仅可能是取消/关闭和本 Lane 的异步操作交错；重新读取即可。
                    continue

                # take 成功后消息已不再属于 pending；它只在本 Lane 内存的
                # TurnOperation 中存活。异常退出不会自动重放，符合 at-most-once 语义。
                item = taken
                inbound = self._to_inbound(item, channel_id)
                turn_id = f"turn_{uuid.uuid4().hex[:12]}"
                # active 只存在 Lane 内存：request 已从 pending 领取，随后
                # TurnExecutor 立即将它写成 UserMsg。进程若在这之间退出，允许丢失。
                operation = TurnOperation(item=item, turn_id=turn_id, task=None)
                self._operation = operation
                task = asyncio.create_task(
                    self._executor.execute(
                        inbound,
                        turn_id=turn_id,
                        config=config,
                        agent_profile=profile,
                    ),
                    name=f"turn:{self.session_id}:{turn_id}",
                )
                operation.task = task
                try:
                    outcome = await task
                except asyncio.CancelledError:
                    # 如果取消正好发生在 TurnExecutor 尚未来得及启动之前，task 会直接
                    # 抛出 CancelledError。它仍是一次正常的“取消当前请求”，不能让
                    # 整个 Lane worker 一并退出、把后续 pending 留在磁盘无人消费。
                    if self._closed:
                        return
                    outcome = TurnOutcome(turn_id=turn_id, status="cancelled")
                finally:
                    if isinstance(self._operation, TurnOperation):
                        self._operation = None

                # pending 在领取时已经减少；本轮完成后刷新状态，使客户端知道
                # 当前 activity 已回到 idle，下一条（若有）由同一 worker 继续领取。
                await self._mailbox.advance_revision(self.session_id)

                # messages 已经保存本轮 User/Assistant 内容；完成结果只用于同进程的
                # task/team 等同步等待，Gateway 重启后不恢复这份瞬时结果。
                await self._completion.complete(self.session_id, item.request_id, outcome)
                await self._publish_snapshot(self.session_id)

                # 70% 关口在 Turn 完整结束后：无论队列中是否还有等待请求都检查。
                # 队列空也预压缩——空闲会话在收尾时把上下文清干净，下一条消息到达
                # 时无需同步等待压缩，客户端气泡也在 turn 结束时立即出现（而不是
                # 等用户再发一条消息触发 before_claim 的 80% 强制水位才压缩）。
                after = await self._context_gate.after_turn(
                    self.session_id, channel_id, config
                )
                if after.action == "compact" and not await self._compact_or_block(
                    channel_id, config, after.reason
                ):
                    return
        except Exception:
            logger.exception("[session-lane] worker 异常 session=%s", self.session_id)
            self._operation = BlockedOperation("SessionLane 内部错误；请求保留在队列中")
            # blocked 只改变内存 operation，不改变 pending；仍需发出一个新版本。
            await self._mailbox.advance_revision(self.session_id)
            await self._publish_snapshot(self.session_id)
        finally:
            self._worker = None

    async def _compact_or_block(self, channel_id: str, config, reason: str) -> bool:
        """压缩期间不 claim 用户请求，因此其内容绝不会提前进入 LLM 上下文。"""
        self._operation = CompactOperation(reason)
        # compacting 是瞬时状态，不保存为磁盘 active；revision 让客户端仍能
        # 区分这张快照与相同 pending 内容下的 idle/running 快照。
        await self._mailbox.advance_revision(self.session_id)
        await self._publish_snapshot(self.session_id)
        decision = await self._context_gate.compact(self.session_id, channel_id, config)
        if decision.action == "block":
            self._operation = BlockedOperation(decision.reason)
            await self._mailbox.advance_revision(self.session_id)
            await self._publish_snapshot(self.session_id)
            return False
        self._operation = None
        await self._mailbox.advance_revision(self.session_id)
        await self._publish_snapshot(self.session_id)
        return True

    def _to_inbound(self, item: QueueItem, channel_id: str) -> BusMessage:
        """从最小 QueueItem 恢复一次进程内 Bus 信封。

        session 和 Channel 从 Lane/Session 取得，不让它们成为第二份持久事实。
        WS frame_id 等传输字段在入口已变成 request_id；Lane 不再知道它们。
        """
        return BusMessage(
            type="user_message",
            from_channel=channel_id,
            from_session=self.session_id,
            to_channel="agent",
            to_session=self.session_id,
            data={
                "session_id": self.session_id,
                "content": item.content,
                "attachments": item.attachments,
            },
            metadata=InboundMetadata(
                request_id=item.request_id,
                agent_id=item.agent_id,
            ),
        )

    async def cancel_active(self, expected_request_id: str = "") -> bool:
        """只取消当前 Turn，绝不清空 pending；取消后 worker 会继续消费队列。"""
        operation = self._operation
        if not isinstance(operation, TurnOperation):
            return False
        if expected_request_id and operation.item.request_id != expected_request_id:
            return False
        if operation.state == "compacting":
            # 摘要是共享一致性操作；普通 /cancel 不允许留下“压缩仍跑、下一轮已开始”。
            return False
        operation.state = "cancelling"
        if operation.task is None:
            return False
        operation.task.cancel()
        # cancelling 不改变 pending，必须单独推进快照版本。
        await self._mailbox.advance_revision(self.session_id)
        await self._publish_snapshot(self.session_id)
        return True

    async def set_turn_compacting(self, value: bool) -> bool:
        """手工 /compact 仍属于当前 Turn，但客户端状态必须准确显示为 compacting。"""
        operation = self._operation
        if not isinstance(operation, TurnOperation):
            return False
        operation.state = "compacting" if value else "running"
        # 手工 /compact 的状态也仅存在内存；同样使用版本推进通知客户端。
        await self._mailbox.advance_revision(self.session_id)
        await self._publish_snapshot(self.session_id)
        return True

    async def cancel_pending(self, request_id: str) -> QueueItem | None:
        """HTTP 的“移除排队消息”入口；取消 BLOCKED 队首也会重新唤醒 worker。"""
        # 这里只允许从 pending 移除，不触碰内存 active；active 必须走 cancel_active，
        # 从而保证取消当前执行不会静默清空用户已经排队的消息。
        item = await self._mailbox.cancel_pending(self.session_id, request_id)
        if item is None:
            return None
        await self._completion.complete(
            self.session_id,
            request_id,
            TurnOutcome(turn_id="", status="cancelled"),
        )
        if isinstance(self._operation, BlockedOperation):
            self._operation = None
            self._ensure_worker()
        await self._publish_snapshot(self.session_id)
        return item

    async def close(self) -> None:
        """关闭栅栏先立起，再停止 worker；close 与 submit 不会交错接纳。"""
        # 关闭必须等待 worker/compaction 真正退出，不能只取消 Task 引用后立即删 session。
        async with self._admission_lock:
            self._closed = True
            worker = self._worker
            operation = self._operation
            if isinstance(operation, TurnOperation) and operation.task is not None:
                operation.task.cancel()
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
        # CompactManager 的普通等待使用 shield。会话关闭时，不论是队列门控压缩，
        # 还是当前 Turn 发起的手工压缩，都必须取消真实任务，避免 worker 停止后
        # 摘要任务仍在后台写入一个已经删除/关闭的会话。
        if isinstance(operation, CompactOperation) or (
            isinstance(operation, TurnOperation) and operation.state == "compacting"
        ):
            await self._context_gate.cancel(self.session_id)
        if worker is not None and worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)
        # stop/close 不重放已经领取的任务；运行态随进程结束，messages 保留已落盘内容。
        await self._completion.close_session(self.session_id)

    async def snapshot(self) -> MailboxState:
        return await self._mailbox.snapshot(self.session_id)


class SessionLaneRegistry:
    """AgentLoop 管理 Lane 生命周期的内部注册表，不向 Channel/HTTP 泄露 Lane。"""

    def __init__(
        self,
        *,
        mailbox: MailboxStore,
        context_gate: ContextGate,
        executor: TurnExecutor,
        completion: CompletionRegistry,
        publish_snapshot: SnapshotPublisher,
    ) -> None:
        self._mailbox = mailbox
        self._context_gate = context_gate
        self._executor = executor
        self._completion = completion
        self._publish_snapshot = publish_snapshot
        self._lanes: dict[str, SessionLane] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        # 删除会话时先写入这道内存关闭栅栏，再等待 Lane 收尾。否则旧 Lane 已被
        # pop、SessionManager 尚未删盘的短窗口里，submit 会错误创建一个新 Lane。
        self._closing_sessions: set[str] = set()

    async def submit(self, inbound: BusMessage) -> AdmissionResult:
        session_id = str(inbound.data.get("session_id") or inbound.from_session or "")
        if not session_id:
            return AdmissionResult(False, "", error={"code": "session_required", "message": "缺少 session_id", "retryable": False})
        async with self._lock:
            # Registry 锁只保护“找到/创建哪一个 Lane”；真正的落盘在 Lane.submit
            # 的 admission_lock 内完成。这样不同 session 可以并行接纳，同一 session
            # 仍只有一个生命周期所有者。
            if self._stopping:
                return AdmissionResult(False, session_id, error={"code": "gateway_stopping", "message": "Gateway 正在停止", "retryable": True})
            if session_id in self._closing_sessions:
                return AdmissionResult(False, session_id, error={"code": "session_closing", "message": "会话正在关闭", "retryable": False})
            lane = self._lanes.get(session_id)
            if lane is None:
                lane = self._new_lane(session_id)
                self._lanes[session_id] = lane
        result = await lane.submit(inbound)
        return result if result.session_id else AdmissionResult(**{**result.__dict__, "session_id": session_id})

    def _new_lane(self, session_id: str) -> SessionLane:
        return SessionLane(
            session_id,
            mailbox=self._mailbox,
            context_gate=self._context_gate,
            executor=self._executor,
            completion=self._completion,
            publish_snapshot=self._publish_snapshot,
        )

    def operation_for(
        self, session_id: str
    ) -> TurnOperation | CompactOperation | BlockedOperation | None:
        """同步读取 Lane 瞬时操作，供 AgentLoop 构造公开快照使用。"""
        lane = self._lanes.get(session_id)
        return lane.operation if lane is not None else None

    async def lane_for(self, session_id: str) -> SessionLane | None:
        """取得可运行 Lane；关闭中的 session 绝不重新创建 Lane。"""
        async with self._lock:
            if self._stopping or session_id in self._closing_sessions:
                return None
            lane = self._lanes.get(session_id)
            if lane is None:
                lane = self._new_lane(session_id)
                self._lanes[session_id] = lane
            return lane

    async def cancel_active(self, session_id: str, expected_request_id: str = "") -> bool:
        lane = await self.lane_for(session_id)
        return await lane.cancel_active(expected_request_id) if lane is not None else False

    async def set_turn_compacting(self, session_id: str, value: bool) -> bool:
        lane = await self.lane_for(session_id)
        return await lane.set_turn_compacting(value) if lane is not None else False

    async def cancel_pending(self, session_id: str, request_id: str) -> QueueItem | None:
        async with self._lock:
            if self._stopping or session_id in self._closing_sessions:
                # 删除栅栏建立后不再写 state.json，避免 HTTP 取消和目录删除交错。
                return None
            lane = self._lanes.get(session_id)
        if lane is not None:
            return await lane.cancel_pending(request_id)
        # 未加载 Lane 的历史 session 仍可通过 HTTP 取消 pending。这里不创建 worker，
        # 只做原子状态变更；下次 recover 才会继续其余请求。
        item = await self._mailbox.cancel_pending(session_id, request_id)
        if item is not None:
            await self._completion.complete(
                session_id,
                request_id,
                TurnOutcome(turn_id="", status="cancelled"),
            )
            await self._publish_snapshot(session_id)
        return item

    async def snapshot(self, session_id: str) -> MailboxState:
        # 快照是持久数据，不因查询/HTTP attach 而创建一个空 Lane。
        return await self._mailbox.snapshot(session_id)

    async def recover(self) -> None:
        """启动时只恢复仍在 pending 的消息；旧运行态绝不重放。"""
        for session_id in await self._mailbox.recoverable_sessions():
            lane = await self.lane_for(session_id)
            if lane is not None:
                lane._ensure_worker()
            await self._publish_snapshot(session_id)

    async def close_session(self, session_id: str) -> None:
        await self.close_sessions((session_id,))

    async def close_sessions(self, session_ids: tuple[str, ...] | list[str]) -> None:
        """原子竖起一组会话的关闭栅栏，再等待所有 Lane 停止。"""
        async with self._lock:
            # 先标记/摘除，再 await Lane.close；任何并发 submit 都会在这里被拒绝，
            # 不会因为 close 的 await 间隙重新创建一个可写的 Lane。
            unique_ids = tuple(dict.fromkeys(session_ids))
            self._closing_sessions.update(unique_ids)
            lanes = [
                lane
                for session_id in unique_ids
                if (lane := self._lanes.pop(session_id, None)) is not None
            ]
        await asyncio.gather(*(lane.close() for lane in lanes), return_exceptions=True)

    async def stop(self) -> None:
        """先拒绝 admission，后关闭全部 Lane，避免 stop 中 worker 复活。"""
        async with self._lock:
            self._stopping = True
            lanes = list(self._lanes.values())
            self._closing_sessions.update(lane.session_id for lane in lanes)
            self._lanes.clear()
        await asyncio.gather(*(lane.close() for lane in lanes), return_exceptions=True)
