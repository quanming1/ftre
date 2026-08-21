"""Stable compaction contract; implementation lives in Feature space."""

from __future__ import annotations

from typing import Any, Protocol


class CompactionPort(Protocol):
    """Public port required by ContextGate, AgentLoop and Command."""

    async def should_compact(
        self,
        session_id: str,
        channel_id: str,
        config: Any,
        *,
        threshold: float | None = None,
        extra_tokens: int = 0,
    ) -> bool: ...

    async def compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config: Any,
        trigger: str = "auto",
        focus_hint: str = "",
    ) -> str | None: ...

    async def compress_fast(
        self,
        session_id: str,
        channel_id: str,
        *,
        config: Any,
        keep_turns: int = 0,
    ) -> bool: ...

    def is_compacting(self, session_id: str) -> bool: ...

    async def cancel_compact(self, session_id: str) -> bool: ...

    async def cancel_all_compact_tasks(self) -> None: ...


class NullCompactionService:
    """No-op port used when the optional Compaction Feature is disabled."""

    async def should_compact(self, *_args, **_kwargs) -> bool:
        return False

    async def compact(self, *_args, **_kwargs) -> str | None:
        return None

    async def compact_if_needed(self, *_args, **_kwargs) -> bool:
        return False

    async def compact_now(self, *_args, **_kwargs) -> str | None:
        return None

    async def compress_fast(self, *_args, **_kwargs) -> bool:
        return False

    def is_compacting(self, _session_id: str) -> bool:
        return False

    async def cancel_compact(self, _session_id: str) -> bool:
        return False

    async def cancel_all_compact_tasks(self) -> None:
        return None
