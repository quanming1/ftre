"""Session 单文件快照与一次性旧格式导入。

运行时 Event 由 ``SessionLog`` 保存在内存，SnapshotCoordinator 只在固定窗口或
语义边界调用快照回调。文件写入复用 JsonStateStore 的原子替换，不把流式 chunk
拆成磁盘记录。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEGACY_LOG_FILE_NAME = "session.jsonl"
_LEGACY_ROW_TYPES = {
    "text-chunks": "text",
    "thinking-chunks": "thinking",
    "tool-result-chunks": "tool_result_text",
}


@dataclass
class _PendingSnapshot:
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    dirty: bool = False
    immediate: bool = False
    generation: int = 0
    waiters: list[asyncio.Future[None]] = field(default_factory=list)


Snapshotter = Callable[[str], Awaitable[None]]


class SnapshotCoordinator:
    """固定窗口的 per-session checkpoint 协调器。

    第一个事件启动计时器，后续 chunk 只增加 generation，不延长窗口；语义事件
    可以把同一窗口升级为立即写入。快照失败时 dirty 保留，下一次事件或显式
    flush 会重试，绝不把失败报告成成功。
    """

    def __init__(
        self,
        snapshotter: Snapshotter,
        *,
        interval_ms: int = 500,
        close_timeout_s: float = 5.0,
    ) -> None:
        self._snapshotter = snapshotter
        self._interval_s = max(0.01, interval_ms / 1000)
        self._close_timeout_s = max(0.1, close_timeout_s)
        self._states: dict[str, _PendingSnapshot] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def mark_dirty(self, session_id: str, *, immediate: bool = False) -> None:
        state = self._states.setdefault(session_id, _PendingSnapshot())
        state.dirty = True
        state.generation += 1
        if immediate:
            state.immediate = True
        state.wake.set()
        task = self._tasks.get(session_id)
        if task is None or task.done():
            self._tasks[session_id] = asyncio.create_task(
                self._run(session_id), name=f"session-snapshot:{session_id}"
            )

    async def flush(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None or not state.dirty:
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                await asyncio.shield(task)
            return

        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        state.waiters.append(done)
        state.immediate = True
        state.wake.set()
        task = self._tasks.get(session_id)
        if task is None or task.done():
            self._tasks[session_id] = asyncio.create_task(
                self._run(session_id), name=f"session-snapshot:{session_id}"
            )
        try:
            await asyncio.shield(done)
        except asyncio.CancelledError:
            if not done.done():
                done.cancel()
            raise

    def detach(self, session_id: str) -> None:
        state = self._states.pop(session_id, None)
        if state is not None:
            for waiter in state.waiters:
                if not waiter.done():
                    waiter.cancel()
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        for session_id, state in list(self._states.items()):
            if not state.dirty:
                continue
            try:
                await asyncio.wait_for(
                    self.flush(session_id), timeout=self._close_timeout_s
                )
            except Exception:
                logger.exception(
                    "[session-snapshot] close flush failed session=%s", session_id
                )
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._states.clear()

    async def _run(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None:
            return
        try:
            while state.dirty:
                generation = state.generation
                if not state.immediate:
                    try:
                        await asyncio.wait_for(state.wake.wait(), self._interval_s)
                    except TimeoutError:
                        pass
                state.wake.clear()
                state.immediate = False
                if not state.dirty:
                    break
                try:
                    await self._snapshotter(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "[session-snapshot] checkpoint failed session=%s", session_id
                    )
                    waiters, state.waiters = state.waiters, []
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_exception(exc)
                    # 保留 dirty，下一次显式 flush/事件重试。
                    await asyncio.sleep(min(self._interval_s, 1.0))
                    continue

                if state.generation == generation:
                    state.dirty = False
                    waiters, state.waiters = state.waiters, []
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(None)
                else:
                    # 写入过程中又有事件，立即处理新 generation，不再等待一个窗口。
                    state.immediate = True
        finally:
            current = self._tasks.get(session_id)
            if current is asyncio.current_task():
                self._tasks.pop(session_id, None)


def legacy_log_path(session_dir: Path) -> Path:
    return session_dir / LEGACY_LOG_FILE_NAME


def _decode_legacy_row(record: dict[str, Any]) -> list[dict[str, Any]]:
    row_type = record.get("type")
    kind = _LEGACY_ROW_TYPES.get(row_type)
    if kind is None:
        return [record]
    required = {"type", "seq0", "time0", "message_id", "data"}
    if set(record) != required:
        raise ValueError("损坏的旧 chunk row：字段不完整")
    data = record["data"]
    if (
        not isinstance(record["seq0"], int)
        or not isinstance(record["time0"], int)
        or not isinstance(data, dict)
        or set(data) != {"kind", "block_id", "tool_call_id", "dt", "deltas"}
        or data.get("kind") != kind
    ):
        raise ValueError("损坏的旧 chunk row：字段类型错误")
    deltas = data["deltas"]
    gaps = data["dt"]
    if (
        not isinstance(deltas, list)
        or len(deltas) < 3
        or not all(isinstance(item, str) for item in deltas)
        or not isinstance(gaps, list)
        or len(gaps) != len(deltas) - 1
        or not all(isinstance(item, int) for item in gaps)
    ):
        raise ValueError("损坏的旧 chunk row：delta/dt 长度错误")
    events: list[dict[str, Any]] = []
    timestamp = record["time0"]
    for index, delta in enumerate(deltas):
        if index:
            timestamp += gaps[index - 1]
        events.append(
            {
                "type": "assistant/chunk",
                "seq": record["seq0"] + index,
                "time": timestamp,
                "message_id": record.get("message_id"),
                "data": {
                    "kind": kind,
                    "delta": delta,
                    "block_id": data.get("block_id"),
                    "tool_call_id": data.get("tool_call_id"),
                },
            }
        )
    return events


def read_legacy_events(session_dir: Path) -> list[dict[str, Any]]:
    """读取旧 JSONL，仅供一次迁移；不提供新的 append/write API。"""
    path = legacy_log_path(session_dir)
    if not path.exists():
        return []
    raw_lines = path.read_bytes().splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for raw_line in raw_lines:
        offsets.append(cursor)
        cursor += len(raw_line)
    nonempty = [i for i, line in enumerate(raw_lines) if line.strip()]
    last_nonempty = nonempty[-1] if nonempty else -1
    events: list[dict[str, Any]] = []
    for index, raw_line in enumerate(raw_lines):
        try:
            line = raw_line.decode("utf-8-sig" if index == 0 else "utf-8").strip()
            if not line:
                continue
            parsed = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == last_nonempty:
                logger.warning("[session-snapshot] truncate legacy torn tail: %s", path)
                with path.open("r+b") as handle:
                    handle.truncate(offsets[index])
                    handle.flush()
                    os.fsync(handle.fileno())
                break
            raise ValueError(f"旧会话日志中段损坏: {path} line={index + 1}") from None
        if index == 0 and isinstance(parsed, dict) and parsed.get("format") == "ftre-session-log":
            if parsed.get("v") != 1:
                raise ValueError(f"不支持的旧会话日志版本: {parsed.get('v')!r}")
            continue
        if not isinstance(parsed, dict):
            raise TypeError(f"旧事件行不是对象: {path} line={index + 1}")
        if parsed.get("type") in _LEGACY_ROW_TYPES:
            events.extend(_decode_legacy_row(parsed))
        elif "type" in parsed and "seq" in parsed:
            events.append(parsed)
        else:
            raise ValueError(f"旧事件行缺少必要字段: {path} line={index + 1}")
    return events


def repair_legacy_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为旧日志中断的工具/turn 生成一次性恢复状态。"""
    result = list(events)
    open_tool_calls: dict[str, tuple[str, str]] = {}
    open_turn: dict[str, Any] | None = None
    assistant_id: str | None = None
    for event in events:
        type_ = event.get("type")
        data = event.get("data") or {}
        if type_ == "turn/start":
            open_turn = dict(data)
            assistant_id = None
            open_tool_calls = {}
        elif type_ == "turn/end":
            open_turn = None
            assistant_id = None
            open_tool_calls = {}
        elif type_ == "tool/call-start" and open_turn is not None:
            assistant_id = str(event.get("message_id") or assistant_id or "")
            tool_id = str(data.get("tool_call_id") or "")
            if tool_id:
                open_tool_calls[tool_id] = (str(data.get("name") or ""), assistant_id)
        elif type_ in {"assistant/chunk", "assistant/message", "hint/message"} and open_turn is not None:
            if event.get("message_id"):
                assistant_id = str(event["message_id"])
        elif type_ == "tool/result":
            open_tool_calls.pop(str(data.get("tool_call_id") or ""), None)
    if open_turn is None and not open_tool_calls:
        return result
    now = int(time.time() * 1000)
    next_seq = len(events)
    for tool_id, (name, owner) in open_tool_calls.items():
        result.append(
            {
                "type": "tool/result",
                "seq": next_seq,
                "time": now,
                "message_id": owner or None,
                "data": {
                    "tool_call_id": tool_id,
                    "name": name,
                    "output": [{"type": "text", "text": "[CRASHED] 执行中断，结果未知"}],
                    "state": "interrupted",
                    "metadata": {"synthetic": True},
                },
            }
        )
        next_seq += 1
    if open_turn is not None:
        result.append(
            {
                "type": "turn/end",
                "seq": next_seq,
                "time": now,
                "message_id": assistant_id,
                "data": {
                    "turn_id": str(open_turn.get("turn_id") or ""),
                    "request_id": str(open_turn.get("request_id") or ""),
                    "outcome": "cancelled",
                    "reason": "crashed",
                    "error": {"code": "crashed", "message": "Gateway 异常退出，turn 由 migration 收尾"},
                    "usage": None,
                    "iterations": 0,
                    "metadata": {"synthetic": True},
                },
            }
        )
    return result


__all__ = [
    "LEGACY_LOG_FILE_NAME",
    "SnapshotCoordinator",
    "legacy_log_path",
    "read_legacy_events",
    "repair_legacy_events",
]
