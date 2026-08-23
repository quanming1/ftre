"""同一 Gateway 进程内按 request_id 等待 Turn 结束。

它是纯内存同步原语，不是第二份持久化状态：Gateway 重启后等待调用栈同样消失，
不会尝试恢复或重放旧 Turn。最近结果只保留有限缓存，用来覆盖“提交后极快完成、
调用方随后才开始 wait”的正常竞态。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict

from .turn_executor import TurnOutcome


class CompletionRegistry:
    """一个 request 可有多个同进程等待者，完成结果只投递一次。"""

    _CACHE_LIMIT = 256

    def __init__(self) -> None:
        self._waiters: dict[tuple[str, str], list[asyncio.Future[TurnOutcome]]] = defaultdict(list)
        self._cache: OrderedDict[tuple[str, str], TurnOutcome] = OrderedDict()
        self._lock = asyncio.Lock()

    async def complete(
        self, session_id: str, request_id: str, outcome: TurnOutcome
    ) -> None:
        """通知同进程等待者；调用时 Turn 已结束，messages 已是聊天事实源。"""
        key = (session_id, request_id)
        async with self._lock:
            self._cache[key] = outcome
            self._cache.move_to_end(key)
            while len(self._cache) > self._CACHE_LIMIT:
                self._cache.popitem(last=False)
            waiters = self._waiters.pop(key, [])
        for future in waiters:
            if not future.done():
                future.set_result(outcome)

    async def wait(self, session_id: str, request_id: str) -> TurnOutcome:
        """等待指定请求；先查内存缓存再登记 waiter，避免正常竞态漏唤醒。"""
        key = (session_id, request_id)
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            future: asyncio.Future[TurnOutcome] = asyncio.get_running_loop().create_future()
            self._waiters[key].append(future)
        try:
            return await future
        finally:
            async with self._lock:
                waiters = self._waiters.get(key)
                if waiters and future in waiters:
                    waiters.remove(future)
                    if not waiters:
                        self._waiters.pop(key, None)

    async def close_session(self, session_id: str) -> None:
        """会话关闭时明确唤醒 waiter，避免 task/team 永远悬挂。"""
        async with self._lock:
            keys = [key for key in self._waiters if key[0] == session_id]
            waiters = [future for key in keys for future in self._waiters.pop(key)]
            cached_keys = [key for key in self._cache if key[0] == session_id]
            for key in cached_keys:
                self._cache.pop(key, None)
        for future in waiters:
            if not future.done():
                future.set_exception(RuntimeError(f"session 已关闭: {session_id}"))

    async def close(self) -> None:
        """关闭整个 Loop 的等待注册表，避免 shutdown 后遗留 Future/结果缓存。"""
        async with self._lock:
            waiters = [future for futures in self._waiters.values() for future in futures]
            self._waiters.clear()
            self._cache.clear()
        for future in waiters:
            if not future.done():
                future.set_exception(RuntimeError("AgentLoop 已关闭"))
