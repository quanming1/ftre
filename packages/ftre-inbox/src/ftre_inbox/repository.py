"""Inbox 的单一持久化 Owner。"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .models import InboxSnapshot, QueueItem, QueueTarget, _MutableInbox


class InboxRepository:
    """按 Session 原子读写 pending 队列。claim 后项目永久离开队列。"""

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
        self.capacity = capacity
        self._session_exists = session_exists
        self._request_seen = request_seen
        self.legacy_root = Path(legacy_root) if legacy_root is not None else None
        self._states: dict[str, _MutableInbox] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def load_all(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await self._migrate_legacy_mailboxes()
        for path in self.root.glob("*/inbox.json"):
            try:
                raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
                state = _MutableInbox.from_json(raw)
                self._states[state.session_id] = state
                if raw.get("schema_version") != 3 or raw.get("inflight"):
                    await self._commit(state)
            except Exception:  # noqa: BLE001 - isolate one broken Inbox
                corrupt = path.with_name(f"inbox.json.corrupt-{uuid.uuid4().hex[:8]}")
                await asyncio.to_thread(path.rename, corrupt)

    async def _migrate_legacy_mailboxes(self) -> None:
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
                    state.next_turn.append(
                        QueueItem(
                            request_id=str(value["request_id"]),
                            sequence=int(value["sequence"]),
                            session_id=session_id,
                            channel_id=str(session.get("channel_id") or ""),
                            content=str(value.get("content", "")),
                            attachments=tuple(dict(item) for item in value.get("attachments", ())),
                            agent_id=str(value.get("agent_id") or "default"),
                        )
                    )
                state.validate()
                await self._commit(state)
                raw.pop("mailbox", None)
                await asyncio.to_thread(
                    self._atomic_write,
                    state_path,
                    json.dumps(raw, ensure_ascii=False, indent=2),
                )
            except (OSError, TypeError, ValueError, KeyError):
                continue

    def recoverable_sessions(self) -> list[str]:
        return [sid for sid, state in self._states.items() if state.next_turn or state.next_step]

    def close(self) -> None:
        self._states.clear()
        self._locks.clear()

    async def snapshot(self, session_id: str) -> InboxSnapshot:
        async with self.lock_for(session_id):
            return self._state(session_id).snapshot(self.capacity)

    async def admit(self, item: QueueItem, target: QueueTarget) -> tuple[bool, int]:
        if self._session_exists is not None and not self._session_exists(item.session_id):
            raise ValueError(f"session 不存在: {item.session_id}")
        async with self.lock_for(item.session_id):
            state = self._state(item.session_id)
            existing = self._find(state, item.request_id)
            if existing is not None:
                return False, existing[1] + 1
            if self._request_seen is not None and self._request_seen(item.session_id, item.request_id):
                return False, 0
            if len(state.next_turn) + len(state.next_step) >= self.capacity:
                raise OverflowError(f"Inbox 已满: {self.capacity}")
            queued = copy.deepcopy(item)
            if queued.sequence <= 0:
                queued = replace(queued, sequence=state.next_sequence)
            state.next_sequence = max(state.next_sequence, queued.sequence + 1)
            target_items = state.next_turn if target == "next-turn" else state.next_step
            target_items.append(queued)
            state.revision += 1
            state.validate()
            await self._commit(state)
            return True, len(state.next_turn) + len(state.next_step)

    async def claim(self, session_id: str, request_ids: tuple[str, ...]) -> tuple[QueueItem, ...]:
        """原子移除指定项目；调用方拿到项目后直接交给 AgentService。"""
        if not request_ids:
            return ()
        async with self.lock_for(session_id):
            state = self._state(session_id)
            locations: list[tuple[list[QueueItem], int]] = []
            for request_id in request_ids:
                hit = self._find(state, request_id)
                if hit is None:
                    return ()
                locations.append(hit)
            claimed = tuple(items[index] for items, index in locations)
            for items, index in sorted(locations, key=lambda value: value[1], reverse=True):
                items.pop(index)
            state.revision += 1
            state.validate()
            await self._commit(state)
            return claimed

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

    async def edit(
        self,
        session_id: str,
        request_id: str,
        content: str,
        attachments=None,
    ) -> QueueItem | None:
        async with self.lock_for(session_id):
            state = self._state(session_id)
            hit = self._find(state, request_id)
            if hit is None:
                return None
            items, index = hit
            items[index] = replace(
                items[index],
                content=content,
                attachments=tuple(dict(item) for item in (attachments or ())),
                messages=(),
            )
            state.revision += 1
            await self._commit(state)
            return items[index]

    async def promote(
        self,
        session_id: str,
        request_id: str,
        *,
        target_run_id: str | None = None,
    ) -> QueueItem | None:
        async with self.lock_for(session_id):
            state = self._state(session_id)
            hit = self._find(state, request_id)
            if hit is None:
                return None
            items, index = hit
            item = items[index]
            if items is state.next_step:
                if target_run_id and item.target_run_id is None:
                    item = replace(item, target_run_id=target_run_id)
                    items[index] = item
                    state.revision += 1
                    await self._commit(state)
                return item
            items.pop(index)
            item = replace(
                item,
                source="user" if item.source != "user" else item.source,
                target_run_id=target_run_id or item.target_run_id,
            )
            state.next_step.append(item)
            state.next_step.sort(key=lambda value: value.sequence)
            state.revision += 1
            state.validate()
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
        await asyncio.to_thread(
            self._atomic_write,
            self.path_for(state.session_id),
            json.dumps(state.to_json(), ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


__all__ = ["InboxRepository"]
