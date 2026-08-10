"""
Agent 生命周期事件中心（挂在 AgentLoop 实例上）

与 EventBus 的分工：
- EventBus      → 消息传输（BusMessage 进出队列，跨 channel 分发）
- AgentEventHub → 进程内生命周期事件（agent 开始/完成/取消），同步 emit

两类订阅者：
- wait(session_id, event)   → 一次性等待（返回 concurrent Future，跨线程可用；
                              task/team 工具在工具线程阻塞 fut.result()）
- subscribe(event, cb)      → 持续订阅（事件循环线程回调；预留接口，
                              当前 team/task 用不到，后续事件功能再实现消费方）

线程模型：
- wait()/unregister() 在工具线程调用
- emit()/cancel_all() 在事件循环线程调用
- 两个线程可能同时改 _waiters，因此用 threading.Lock 保护；
  concurrent.futures.Future 的 result()/set_result() 线程安全，可跨线程唤醒
"""
import logging
import threading
from concurrent.futures import Future
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AgentEventHub:
    """Agent 生命周期事件中心。

    事件是一次性的：emit() 取走并唤醒所有匹配的等待者后即清除，
    下一轮完成需要重新 wait()（多轮 team_say / task 天然支持）。
    """

    # ── 事件名常量（新增事件在此登记）──
    # payload 约定：{session_id, status, final_content}
    AGENT_FINISHED = "agent_finished"
    AGENT_STARTED = "agent_started"     # 预留：agent 开始运行
    AGENT_CANCELLED = "agent_cancelled"  # 预留：agent 被取消

    def __init__(self) -> None:
        # (session_id, event) → set[Future]：一次性等待者
        self._waiters: dict[tuple[str, str], set[Future]] = {}
        # event → set[callback]：持续订阅者
        self._subscribers: dict[str, set[Callable[[str, dict], Any]]] = {}
        self._lock = threading.Lock()

    # ── 等待方（工具线程调用）────────────────────────────────

    def wait(self, session_id: str, event: str) -> Future | None:
        """注册一次性等待；该 session+事件已有未完成等待者时返回 None。

        返回的 Future 会在 emit 时被 set_result(payload)，
        调用方在任何线程 fut.result() 都能拿到 payload。
        """
        fut: Future = Future()
        key = (session_id, event)
        with self._lock:
            waiters = self._waiters.setdefault(key, set())
            for existing in waiters:
                if not existing.done():
                    return None  # 已有等待者，拒绝重复等待
            waiters.add(fut)
        return fut

    def unregister(self, session_id: str, event: str, future: Future) -> None:
        """移除等待者（超时/跳过路径主动清理，防残留）。"""
        key = (session_id, event)
        with self._lock:
            waiters = self._waiters.get(key)
            if waiters:
                waiters.discard(future)
                if not waiters:
                    self._waiters.pop(key, None)

    # ── 生产方（事件循环线程调用）────────────────────────────

    def emit(self, session_id: str, event: str, payload: dict) -> None:
        """发布事件：唤醒所有匹配的一次性等待者 + 通知持续订阅者。

        无条件广播（不依赖通道名推断）——谁等谁订阅，
        没有等待者时无人消费，零开销。
        """
        key = (session_id, event)
        with self._lock:
            waiters = self._waiters.pop(key, None)  # 一次性：取走即清
            subscribers = list(self._subscribers.get(event, ()))
        if waiters:
            for fut in waiters:
                if not fut.done():
                    fut.set_result(payload)  # 线程安全，工具线程 fut.result() 立即返回
        for cb in subscribers:
            try:
                cb(session_id, payload)
            except Exception:
                # 订阅者异常不杀生产方（_finalize 是 Turn 唯一收尾出口，不能抛）
                logger.exception("[event-hub] 订阅者回调异常: %s.%s", session_id, event)

    # ── 生命周期兜底 ────────────────────────────────────────

    def cancel_all(self, payload: dict) -> None:
        """AgentLoop.stop() 时唤醒全部残留等待者（cancelled 兜底）。"""
        with self._lock:
            pending = self._waiters
            self._waiters = {}
        for fut in {f for s in pending.values() for f in s}:
            if not fut.done():
                fut.set_result(payload)

    # ── 持续订阅（预留接口）──────────────────────────────────

    def subscribe(self, event: str, callback: Callable[[str, dict], Any]) -> None:
        """注册持续订阅者：每次 emit 该事件时回调 (session_id, payload)。

        当前 team/task 用不到（它们是一次性 wait），
        未来桌面端"agent 完成提示"、trace 记录等直接订阅即可。
        """
        with self._lock:
            self._subscribers.setdefault(event, set()).add(callback)
