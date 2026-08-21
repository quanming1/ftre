"""AgentLoop 到公开 AgentDriver 的单向适配。"""

from __future__ import annotations

from typing import Any

from ftre.services.agent.contracts import AgentDriver


class AgentLoopDriver(AgentDriver):
    """只暴露 AgentService 需要的显式方法，不暴露 Loop 属性或私有组件。"""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    def is_session_busy(self, session_id: str) -> bool:
        return self._loop.is_session_busy(session_id)

    def get_session_status(self, session_id: str) -> str:
        return self._loop.get_session_status(session_id)

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        return await self._loop.submit_inbound(*args, **kwargs)

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        return await self._loop.cancel_session(*args, **kwargs)

    async def wait(self, *args: Any, **kwargs: Any) -> Any:
        return await self._loop.wait_request(*args, **kwargs)

    async def delete_session(self, session_id: str) -> Any:
        return await self._loop.delete_session(session_id)

    async def cancel_queued_message(self, session_id: str, request_id: str) -> Any:
        return await self._loop.cancel_queued_message(session_id, request_id)

    async def get_mailbox_snapshot(self, session_id: str) -> Any:
        return await self._loop.get_mailbox_snapshot(session_id)


__all__ = ["AgentLoopDriver"]
