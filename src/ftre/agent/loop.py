"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 对不同 session 并发执行，对同一 session 用 asyncio.Lock 串行
- 系统级指令绕过 session lock，在锁外直接执行
- Agent 执行全在主事件循环，Task.cancel() 在 LLM stream 的 await 处立即生效

Turn 执行逻辑（状态机驱动）已拆到 TurnExecutor，
AgentLoop 只管消费循环 + 并发控制 + 生命周期。
"""

import asyncio
import logging
from concurrent.futures import Future

from ftre_agent_core import Tracer
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.hooks import FtreCoreHookManager
from ftre_agent_core.tool import ToolRegistry

from ftre.bus import BusMessage, EventBus
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID
from ftre.config import AgentConfig
from ftre.session import SessionManager
from ftre.trace_store import TRACE_DB_PATH, SQLiteTraceExporter

from .compact_manager import CompactManager
from .turn_executor import TurnExecutor

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全局单例，消费所有 session 的消息。

    并发模型：
    - _consume：只负责从 inbound 队列取消息，create_task 派发后立即取下一条
    - TurnExecutor.execute：状态机驱动，per-session asyncio.Lock 保证同一 session 串行
    - 系统级指令（如 /cancel）：绕过 session lock，在锁外直接执行
    - 所有 Agent 执行在主事件循环，Task.cancel() 可在 LLM stream 的 await 处立即生效

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent + 取消所有 dispatch task
    """

    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionManager,
        channel_manager=None,
        config: AgentConfig = None,
        hook_manager=None,
        core_hook_manager: FtreCoreHookManager | None = None,
        tool_registry: ToolRegistry | None = None,
        command_manager=None,
        plugin_manager=None,
        agent_manager=None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.hook_manager = hook_manager
        self.core_hook_manager = core_hook_manager or FtreCoreHookManager()
        self.tool_registry = tool_registry
        self.command_manager = command_manager
        self.plugin_manager = plugin_manager
        self.agent_manager = agent_manager
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self.tracer = Tracer([SQLiteTraceExporter(TRACE_DB_PATH)])

        # ─── 并发控制 ──────────────────────────────────────────
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_agents: dict[str, ReActAgent] = {}
        self._subagent_done_futures: dict[str, Future[dict]] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._compacting_sessions: set[str] = set()

        # ─── Turn 执行器 ──────────────────────────────────────
        self._executor = TurnExecutor(self)

        self.compact_manager = CompactManager(
            session_manager=self.session_manager,
            bus=self.bus,
            threshold=self._initial_context_cfg().compact_threshold,
        )

    def _initial_context_cfg(self):
        """实例化时读一次 ContextConfig 用于 CompactManager 默认参数。"""
        try:
            cfg = self._load_current_config()
            return cfg.context
        except Exception:
            from ftre.config import ContextConfig

            return ContextConfig()

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return session_id in self._active_agents

    def get_session_status(self, session_id: str) -> str:
        """返回客户端可见状态：idle / running / compacting。"""
        if session_id in self._active_agents:
            return "running"
        if session_id in self._compacting_sessions:
            return "compacting"
        return "idle"

    def register_subagent_done_future(
        self, session_id: str, future: Future[dict]
    ) -> bool:
        """注册 subagent 单轮执行完成通知；返回 False 表示已有等待者。"""
        existing = self._subagent_done_futures.get(session_id)
        if existing is not None and not existing.done():
            return False
        self._subagent_done_futures[session_id] = future
        return True

    def unregister_subagent_done_future(
        self,
        session_id: str,
        future: Future[dict] | None = None,
    ) -> None:
        """移除未完成的 subagent 等待者，避免启动/执行超时后残留。"""
        existing = self._subagent_done_futures.get(session_id)
        if existing is not None and (future is None or existing is future):
            self._subagent_done_futures.pop(session_id, None)

    async def stop(self) -> None:
        """优雅关闭：取消消费循环 + 中断所有 Agent。"""
        if self._task:
            self._task.cancel()

        for t in list(self._dispatch_tasks):
            t.cancel()

        for agent in self._active_agents.values():
            agent.cancel_nowait()
        self._active_agents.clear()
        self._session_tasks.clear()

        self.compact_manager.cancel_all_compact_tasks()
        for sid, future in self._subagent_done_futures.items():
            if not future.done():
                future.set_result(
                    {
                        "session_id": sid,
                        "channel_id": SUBAGENT_CHANNEL_ID,
                        "status": "cancelled",
                        "final_content": "",
                    }
                )
        self._subagent_done_futures.clear()

    # ─── 消费循环 ────────────────────────────────────────────

    async def _consume(self) -> None:
        """消费循环：从 inbound 队列取消息，并发派发到 TurnExecutor.execute。"""
        try:
            async for msg in self.bus.subscribe_inbound():
                try:
                    task = asyncio.create_task(self._executor.execute(msg))
                    self._dispatch_tasks.add(task)
                    task.add_done_callback(self._dispatch_tasks.discard)
                except Exception:
                    logger.exception("[agent-loop] 派发 dispatch 异常，已丢弃该消息")
        except asyncio.CancelledError:
            pass

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置（委托给 TurnExecutor）。"""
        return self._executor._load_current_config()
