"""Inbox 的独立 JSON Repository。

它不读写 SessionService 的 ``state.json``，每个 Session 的 pending 数据放在独立的
``inbox.json``。这样删除或替换 Inbox Package 不会让 AgentService 携带队列模型。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from .models import InboxSnapshot, QueueItem, QueueTarget, _MutableInbox

logger = logging.getLogger(__name__)


class InboxRepository:
    """为每个 Session 提供原子 Inbox 快照和 mutation。"""

    def __init__(
        self,
        root: str | Path,
        *,
        capacity: int = 100,
        session_exists: Callable[[str], bool] | None = None,
        request_seen: Callable[[str, str], bool] | None = None,
        legacy_root: str | Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.legacy_root = Path(legacy_root) if legacy_root is not None else None
        self.capacity = capacity
        self._session_exists = session_exists
        self._request_seen = request_seen
        self._states: dict[str, _MutableInbox] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def load_all(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await self._migrate_legacy_mailboxes()
        for path in self.root.glob("*/inbox.json"):
            try:
                payload = await asyncio.to_thread(path.read_text, encoding="utf-8")
                state = _MutableInbox.from_json(json.loads(payload))
            except Exception:  # noqa: BLE001 - isolate one broken Inbox
                # 不把一个损坏队列当成空队列；保留文件供诊断，跳过该 Session。
                path.rename(path.with_name(f"inbox.json.corrupt-{uuid.uuid4().hex[:8]}"))
                continue
            self._states[state.session_id] = state

    async def _migrate_legacy_mailboxes(self) -> None:
        """一次性把旧 state.json.mailbox 转成独立 inbox.json。

        迁移成功后从 state.json 删除 mailbox 字段；旧 AgentLoop fallback 因而不会在
        Inbox Package 已接管时重复执行同一批 pending。其它 Session 字段原样保留。
        """
        if self.legacy_root is None or not self.legacy_root.exists():
            return
        for state_path in self.legacy_root.glob("*/state.json"):
            session_id = state_path.parent.name
            try:
                raw = json.loads(await asyncio.to_thread(state_path.read_text, encoding="utf-8"))
                mailbox = raw.get("mailbox")
                if not isinstance(mailbox, dict):
                    continue
                session = raw.get("session") if isinstance(raw.get("session"), dict) else {}
                channel_id = str(session.get("channel_id") or "")
                state = _MutableInbox(
                    session_id=session_id,
                    revision=int(mailbox.get("revision", 0)),
                    next_sequence=int(mailbox.get("next_sequence", 1)),
                )
                pending = mailbox.get("pending", ())
                if not isinstance(pending, list):
                    raise TypeError("mailbox.pending 必须是数组")
                for value in pending:
                    if not isinstance(value, dict):
                        raise TypeError("mailbox.pending 项必须是对象")
                    state.next_turn.append(QueueItem(
                        request_id=str(value["request_id"]),
                        sequence=int(value["sequence"]),
                        session_id=session_id,
                        channel_id=channel_id,
                        content=str(value.get("content", "")),
                        attachments=tuple(dict(item) for item in value.get("attachments", ())),
                        source="user",
                    ))
                state.validate()
                # 同一个旧 state 在“新 Inbox 已写入、旧字段尚未删除”的崩溃窗口
                # 中会再次经过这里；覆盖同一 session 的相同快照是幂等的。
                await self._commit(state)
                raw.pop("mailbox", None)
                await asyncio.to_thread(
                    self._atomic_write,
                    state_path,
                    json.dumps(raw, ensure_ascii=False, indent=2),
                )
            except (OSError, TypeError, ValueError, KeyError) as exc:
                # 迁移失败不删除旧 mailbox，下一次启动仍可重试并且不会丢数据。
                logger.warning("[ftre-inbox] legacy migration skipped session=%s: %s", session_id, exc)
                continue

    def recoverable_sessions(self) -> list[str]:
        return [sid for sid, state in self._states.items() if state.next_turn or state.next_step]

    def close(self) -> None:
        """丢弃进程内快照和锁，但保留磁盘文件供下次启用恢复。"""
        self._states.clear()
        self._locks.clear()

    async def snapshot(self, session_id: str) -> InboxSnapshot:
        async with self.lock_for(session_id):
            return self._state(session_id).snapshot(self.capacity)

    async def admit(
        self,
        item: QueueItem,
        target: QueueTarget,
    ) -> tuple[bool, int]:
        """原子接纳；返回 (created, sequence-position)。"""
        if self._session_exists is not None and not self._session_exists(item.session_id):
            raise ValueError(f"session 不存在: {item.session_id}")
        async with self.lock_for(item.session_id):
            state = self._state(item.session_id)
            existing = self._find(state, item.request_id)
            if existing is not None:
                return False, existing[1] + 1
            if self._request_seen is not None and self._request_seen(
                item.session_id, item.request_id
            ):
                return False, 0
            if len(state.next_turn) + len(state.next_step) >= self.capacity:
                raise OverflowError(f"Inbox 已满: {self.capacity}")
            queued = copy.deepcopy(item)
            if queued.sequence <= 0:
                queued = QueueItem(
                    request_id=queued.request_id,
                    sequence=state.next_sequence,
                    session_id=queued.session_id,
                    channel_id=queued.channel_id,
                    content=queued.content,
                    attachments=queued.attachments,
                    source=queued.source,
                    history_message_id=queued.history_message_id,
                )
            state.next_sequence = max(state.next_sequence, queued.sequence + 1)
            (state.next_turn if target == "next-turn" else state.next_step).append(queued)
            state.validate()
            state.revision += 1
            await self._commit(state)
            return True, len(state.next_turn) + len(state.next_step)

    async def claim(
        self,
        session_id: str,
        request_ids: tuple[str, ...],
    ) -> tuple[QueueItem, ...]:
        """按精确 ID 原子领取；任何 ID 不在队列中则整体失败。"""
        async with self.lock_for(session_id):
            state = self._state(session_id)
            found: list[QueueItem] = []
            locations: list[tuple[list[QueueItem], int]] = []
            for request_id in request_ids:
                hit = self._find(state, request_id)
                if hit is None:
                    return ()
                locations.append(hit)
                found.append(hit[0][hit[1]])
            for items, index in sorted(locations, key=lambda pair: pair[1], reverse=True):
                items.pop(index)
            state.revision += 1
            await self._commit(state)
            return tuple(found)

    async def remove(self, session_id: str, request_id: str) -> QueueItem | None:
        async with self.lock_for(session_id):
            state = self._state(session_id)
            hit = self._find(state, request_id)
            if hit is None:
                return None
            items, index = hit
            item = items.pop(index)
            state.revision += 1
            await self._commit(state)
            return item

    async def edit(self, session_id: str, request_id: str, content: str, attachments=None) -> QueueItem | None:
        async with self.lock_for(session_id):
            state = self._state(session_id)
            hit = self._find(state, request_id)
            if hit is None:
                return None
            items, index = hit
            old = items[index]
            items[index] = QueueItem(
                request_id=old.request_id,
                sequence=old.sequence,
                session_id=old.session_id,
                channel_id=old.channel_id,
                content=content,
                attachments=tuple(dict(item) for item in (attachments or ())),
                source=old.source,
                history_message_id=old.history_message_id,
            )
            state.revision += 1
            await self._commit(state)
            return items[index]

    async def promote(self, session_id: str, request_id: str) -> QueueItem | None:
        async with self.lock_for(session_id):
            state = self._state(session_id)
            hit = self._find(state, request_id)
            if hit is None:
                return None
            items, index = hit
            if items is state.next_step:
                return items[index]
            item = items.pop(index)
            state.next_step.append(item)
            state.next_step.sort(key=lambda value: value.sequence)
            state.revision += 1
            await self._commit(state)
            return item

    async def delete_session(self, session_id: str) -> None:
        async with self.lock_for(session_id):
            self._states.pop(session_id, None)
            path = self.path_for(session_id)
            if path.exists():
                await asyncio.to_thread(path.unlink)

    def path_for(self, session_id: str) -> Path:
        if not session_id or any(char in session_id for char in "/\\:") or ".." in session_id:
            raise ValueError(f"非法 session_id: {session_id!r}")
        return self.root / session_id / "inbox.json"

    def _state(self, session_id: str) -> _MutableInbox:
        return self._states.setdefault(session_id, _MutableInbox(session_id=session_id))

    @staticmethod
    def _find(state: _MutableInbox, request_id: str) -> tuple[list[QueueItem], int] | None:
        for items in (state.next_turn, state.next_step):
            for index, item in enumerate(items):
                if item.request_id == request_id:
                    return items, index
        return None

    async def _commit(self, state: _MutableInbox) -> None:
        path = self.path_for(state.session_id)
        payload = json.dumps(state.to_json(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._atomic_write, path, payload)

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
