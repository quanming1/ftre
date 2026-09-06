"""session.jsonl 事件日志存储 + write-behind 协调器（PRD-F43 FR3/FR4）。

文件布局：sessions/<sid>/session.jsonl——首行 header
``{"v":1,"format":"ftre-session-log"}``，其后每行一个事件 JSON 或一个可还原的
chunk storage row（append-only）。

写入原子性：首次物化 tmp+fsync+os.replace；后续批追加 + fsync；失败 truncate
回滚到旧 size（防重复 seq）。写序 = 事件序（per-session 串行 flusher +
cursor 连续性校验）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .chunk_rows import decode_storage_record, pack_chunk_runs

logger = logging.getLogger(__name__)

LOG_FILE_NAME = "session.jsonl"
LOG_HEADER = {"v": 1, "format": "ftre-session-log"}
DEFAULT_WRITE_BATCH_MAX_DELAY_MS = 200
DEFAULT_CLOSE_DRAIN_TIMEOUT_S = 5.0


class EventLogError(RuntimeError):
    """事件日志读写一致性错误。"""


def read_event_log(session_dir: Path) -> list[dict[str, Any]]:
    """读取并解析事件日志；torn-tail（末行解析失败）截断并告警。"""
    path = session_dir / LOG_FILE_NAME
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    # 按字节读取而不是 read_text：恢复时必须把损坏末行物理截断，
    # 否则本次启动虽然能继续，下一次启动还会再次读到同一段残片。
    raw_lines = path.read_bytes().splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for raw_line in raw_lines:
        offsets.append(cursor)
        cursor += len(raw_line)
    nonempty_indexes = [
        index for index, raw_line in enumerate(raw_lines)
        if raw_line.strip()
    ]
    last_nonempty_index = nonempty_indexes[-1] if nonempty_indexes else -1

    for index, raw_line in enumerate(raw_lines):
        try:
            line = raw_line.decode("utf-8-sig" if index == 0 else "utf-8").strip()
            if not line:
                continue
            parsed = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == last_nonempty_index:
                logger.warning("[event-log] torn tail 截断 session_dir=%s", session_dir)
                # 保留损坏行之前的完整字节（包括上一行换行符），并同步到磁盘。
                with open(path, "r+b") as handle:
                    handle.truncate(offsets[index])
                    handle.flush()
                    os.fsync(handle.fileno())
                break
            raise EventLogError(f"事件日志中段损坏: {path} line={index + 1}") from None
        if index == 0 and isinstance(parsed, dict) and parsed.get("format") == LOG_HEADER["format"]:
            if parsed.get("v") != 1:
                raise EventLogError(f"不支持的事件日志版本: {parsed.get('v')!r}")
            continue
        if not isinstance(parsed, dict):
            raise EventLogError(f"事件行不是对象: {path} line={index + 1}")
        if parsed.get("type") in {
            "text-chunks",
            "thinking-chunks",
            "tool-result-chunks",
        }:
            try:
                events.extend(decode_storage_record(parsed))
            except (TypeError, ValueError) as exc:
                raise EventLogError(
                    f"chunk storage row 损坏: {path} line={index + 1}: {exc}"
                ) from exc
            continue
        if "type" not in parsed or "seq" not in parsed:
            raise EventLogError(f"事件行缺少必要字段: {path} line={index + 1}")
        events.append(parsed)
    return events


def write_event_log_atomic(session_dir: Path, events: list[dict[str, Any]]) -> None:
    """一次性物化整份日志（fork / 测试用）：tmp + fsync + os.replace。"""
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / LOG_FILE_NAME
    lines = [json.dumps(LOG_HEADER, ensure_ascii=False)]
    for expected_seq, event in enumerate(events):
        if event.get("seq") != expected_seq:
            raise EventLogError(f"seq 不连续: index={expected_seq} seq={event.get('seq')!r}")
    lines.extend(
        json.dumps(record, ensure_ascii=False)
        for record in pack_chunk_runs(events)
    )
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class WriteBehindCoordinator:
    """订阅 SessionLog，批窗口落盘；per-session 串行 flusher。

    flush(session) 是 Service 暴露的强制持久化屏障（同步等待写盘完成）。
    """

    def __init__(
        self,
        session_dir_of: Any,
        *,
        max_delay_ms: int = DEFAULT_WRITE_BATCH_MAX_DELAY_MS,
    ):
        # session_dir_of: callable(session_id) -> Path
        self._session_dir_of = session_dir_of
        self._max_delay_s = max_delay_ms / 1000
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cursors: dict[str, int] = {}  # 已写盘的 next seq

    # ── 订阅入口（SessionLog.notify → enqueue，同步） ────────────

    def attach(self, session_id: str, log) -> None:
        """让一个 SessionLog 的事件进入 write-behind（幂等）。"""
        if session_id in self._queues:
            return
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[session_id] = queue
        # attach 只在 SessionService 完成 load（并把 repair 写回磁盘）后调用。
        # 因此这里的内存前缀确实已经落盘，cursor 才能从事件长度开始。
        self._cursors[session_id] = len(log.events)
        log.subscribe(queue.put_nowait)
        self._ensure_task(session_id)

    def _ensure_task(self, session_id: str) -> None:
        task = self._tasks.get(session_id)
        if task is None or task.done():
            self._tasks[session_id] = asyncio.create_task(
                self._flusher(session_id), name=f"event-log-wb:{session_id}"
            )

    # ── 强制 flush（checkpoint 语义边界） ───────────────────────

    async def flush(self, session_id: str) -> None:
        """等待当前积压全部落盘（幂等；无 attach 时为空操作）。

        一律走 barrier 往返：barrier 排在 flusher 队列尾部，天然排在任何
        in-flight 批次之后——空队列快速路径会漏等 200ms 批窗口内的事件
        （close 前 flush 丢批的根因），故不设快速路径。
        """
        queue = self._queues.get(session_id)
        if queue is None:
            return
        done = asyncio.get_running_loop().create_future()
        queue.put_nowait({"__flush_barrier__": True, "__done__": done})
        try:
            await done
        except asyncio.CancelledError:
            if not done.done():
                done.cancel()
            raise

    async def close(self) -> None:
        """关闭前 drain 全部队列。"""
        for session_id in list(self._queues):
            try:
                await asyncio.wait_for(
                    self.flush(session_id), timeout=DEFAULT_CLOSE_DRAIN_TIMEOUT_S
                )
            except Exception:
                # 第一次写失败时批次仍由 flusher 保存在队首；再给它一次
                # 有界重试窗口，避免 close 立即 cancel 把内存中的事件丢掉。
                logger.exception("[event-log-wb] close flush failed session=%s", session_id)
                try:
                    await asyncio.wait_for(
                        self.flush(session_id), timeout=DEFAULT_CLOSE_DRAIN_TIMEOUT_S
                    )
                except Exception:
                    logger.exception(
                        "[event-log-wb] close drain retry failed session=%s",
                        session_id,
                    )
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._queues.clear()

    def detach(self, session_id: str) -> None:
        """会话删除时丢弃未写完的批次（目录即将消失）。"""
        self._queues.pop(session_id, None)
        self._cursors.pop(session_id, None)
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    # ── flusher ───────────────────────────────────────────────

    async def _flusher(self, session_id: str) -> None:
        queue = self._queues[session_id]
        # 写失败时批次不能重新 put 到队尾，否则后来的 seq 会越过它；
        # 保留在 pending_batch，成功前只重试这个批次。
        pending_batch: list[dict[str, Any]] = []
        try:
            while True:
                if pending_batch:
                    try:
                        await self._write_batch(session_id, pending_batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "[event-log-wb] 重试写盘失败 session=%s batch=%d",
                            session_id, len(pending_batch),
                        )
                        await asyncio.sleep(1.0)
                        continue
                    pending_batch = []

                event = await queue.get()
                batch: list[dict[str, Any]] = []
                barriers: list[asyncio.Future] = []
                if "__flush_barrier__" in event:
                    barriers.append(event["__done__"])
                else:
                    batch.append(event)
                # 批窗口：立即收走当前积压 + 短暂等待聚合（≤200ms）
                deadline = asyncio.get_running_loop().time() + self._max_delay_s
                while True:
                    timeout = deadline - asyncio.get_running_loop().time()
                    try:
                        nxt = await asyncio.wait_for(queue.get(), timeout=max(timeout, 0))
                    except TimeoutError:
                        break
                    if "__flush_barrier__" in nxt:
                        barriers.append(nxt["__done__"])
                        break
                    batch.append(nxt)
                if batch:
                    try:
                        await self._write_batch(session_id, batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception(
                            "[event-log-wb] 写盘失败，保序退避重试 session=%s batch=%d",
                            session_id, len(batch),
                        )
                        pending_batch = batch
                        # flush 不能在数据尚未落盘时报告成功。调用者得到明确
                        # 异常；批次仍在内存中继续保序重试，下一次 flush 可观察成功。
                        for barrier in barriers:
                            if not barrier.done():
                                barrier.set_exception(exc)
                        await asyncio.sleep(1.0)
                        continue
                for barrier in barriers:
                    if not barrier.done():
                        barrier.set_result(None)
        except asyncio.CancelledError:
            return

    async def _write_batch(self, session_id: str, batch: list[dict[str, Any]]) -> None:
        expected = self._cursors.get(session_id)
        if expected is None:
            return
        for index, event in enumerate(batch):
            if event.get("seq") != expected + index:
                raise EventLogError(
                    f"append seq mismatch session={session_id}: "
                    f"expected {expected + index} got {event.get('seq')!r}"
                )
        path = self._session_dir_of(session_id) / LOG_FILE_NAME
        payload = await asyncio.to_thread(self._append_lines, path, batch)
        del payload
        self._cursors[session_id] = expected + len(batch)

    @staticmethod
    def _append_lines(path: Path, batch: list[dict[str, Any]]) -> int:
        """线程池执行的实际写盘；返回写入行数。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(record, ensure_ascii=False)
            for record in pack_chunk_runs(batch)
        ]
        if not path.exists():
            # 首次物化：header + 首批，tmp + fsync + os.replace 原子发布
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(LOG_HEADER, ensure_ascii=False) + "\n")
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return len(lines)
        with open(path, "ab") as handle:
            stat_before = os.fstat(handle.fileno())
            try:
                data = ("\n".join(lines) + "\n").encode("utf-8")
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                # 回滚到旧 size，防半行导致下一批 seq 重复
                handle.truncate(stat_before.st_size)
                handle.flush()
                os.fsync(handle.fileno())
                raise
        return len(lines)


__all__ = [
    "LOG_FILE_NAME",
    "LOG_HEADER",
    "EventLogError",
    "WriteBehindCoordinator",
    "read_event_log",
    "write_event_log_atomic",
]
