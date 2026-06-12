"""
Pipeline — 可注册处理器 / 可短路的通用管线（异步执行）。

handler 接收一个 dict，返回 True 继续、False 短路。handler 可以是同步函数，
也可以是协程函数（async def）；两者都由 run() 统一 await::

    from ftre.utils import Pipeline

    pipe = Pipeline("govern")
    pipe.use(lambda d: d.get("ok") is not False, name="guard")
    pipe.use(work_async, name="work")              # async def work_async(d): ...

    ctx = {"input": "hello"}
    await pipe.run(ctx)
    print(ctx.get("result"))  # "done"
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

Handler = Callable[[dict[str, Any]], bool | Awaitable[bool]]
"""处理器：(data) -> True 继续 / False 短路；可同步可异步。"""


class Pipeline:
    """顺序执行处理器，返回 False 立即短路。支持同步与异步 handler。"""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._steps: list[tuple[str, Handler]] = []

    def use(self, handler: Handler, *, name: str = "") -> "Pipeline":
        self._steps.append((name or f"step_{len(self._steps) + 1}", handler))
        return self

    def sort(self, key=None) -> "Pipeline":
        """按 key 重排步骤。默认按 name 长度降序（长前缀优先）。"""
        self._steps.sort(key=key or (lambda s: -len(s[0])))
        return self

    async def run(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or {}
        for name, handler in self._steps:
            result = handler(data)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                break
        return data

    def steps(self) -> list[str]:
        return [name for name, _ in self._steps]

    def __repr__(self) -> str:
        return f"<Pipeline {self.name!r} steps={len(self._steps)}>"
