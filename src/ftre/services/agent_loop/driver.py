"""AgentLoop 到公开 ``AgentDriver`` 的单向适配。

Driver 有意只保留 AgentService 需要的动作。它不是第二个业务层，也不负责改变
Loop 的并发语义；作用是把具体 Loop 隔离在组合根内，防止 HTTP/Feature 直接持有
运行时对象。
"""

from __future__ import annotations

from typing import Any

from ftre.services.agent.contracts import AgentDriver, InboundMessage


class AgentLoopDriver(AgentDriver):
    """只暴露 AgentService 需要的显式方法，不暴露 Loop 属性或私有组件。"""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    def is_session_busy(self, session_id: str) -> bool:
        return self._loop.is_session_busy(session_id)

    def get_session_status(self, session_id: str) -> str:
        return self._loop.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        return self._loop.is_active_session(session_id)

    async def run(self, message: InboundMessage) -> Any:
        return await self._loop.run_inbound(message)

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        return await self._loop.cancel_session(*args, **kwargs)

    async def delete_session(self, session_id: str) -> Any:
        return await self._loop.delete_session(session_id)

    async def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Any:
        return await self._loop.resume_confirmation(
            session_id,
            channel_id,
            events,
            metadata,
        )


__all__ = ["AgentLoopDriver"]
