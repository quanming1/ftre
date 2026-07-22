"""
TurnExecutor — 单个 Turn 的完整执行。

Turn 是一等公民：一个有状态的生命周期对象，从收到用户消息到响应完成。

状态机驱动：COMMAND → COMPACTING → BUILDING → RUNNING → FINALIZING → COMPLETED。
execute() 只管边界（PIPELINE_START/END）、系统级指令锁外短路、per-session lock。
指令匹配、用户消息存储、普通指令执行都在 COMMAND 状态里做。

处理路径：
  普通消息：  COMMAND(存消息) → COMPACTING → BUILDING → RUNNING → FINALIZING → COMPLETED
  /cancel：   锁外执行取消 → 短路（不进状态机、不存消息）
  /compact：  COMMAND(存消息 + 执行 handler) → COMPLETED（短路）
  RewritePrompt：COMMAND(存消息 + 执行 handler) → COMPACTING → BUILDING → RUNNING → FINALIZING → COMPLETED
"""
import asyncio
import copy
import logging
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum

from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.agent.event import (
    AssistantMessageCompleteEvent,
    StepEvent,
    StepPhase,
    DoneReason,
    ToolResultEvent,
    UserMessageEvent,
)

from ftre.bus import BusMessage, GLOBAL_CHANNEL, GLOBAL_SESSION
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID
from ftre.config import AgentConfig, load_config
from ftre.session.multimodal import build_user_content, normalize_stored_user_content
from ftre.tools._workspace import WorkspaceAccessor

from ftre.command.types import Handled, Passthrough, RewritePrompt, SendMessage

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loop import AgentLoop

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Turn 数据模型
# ═══════════════════════════════════════════════════════════════════

class TurnStatus(str, Enum):
    """Turn 生命周期的阶段。

    状态流转（正常路径）：
        COMMAND → COMPACTING → BUILDING → RUNNING → FINALIZING → COMPLETED
    终态：COMPLETED（正常）/ CANCELLED（被取消）/ ERROR（异常）
    """
    COMMAND = "command"        # 匹配指令 + 存用户消息 + 执行 handler + 路由
    COMPACTING = "compacting"  # 判断是否需要压缩上下文
    BUILDING = "building"      # 鉴权 + 构建消息 + 创建 Agent
    RUNNING = "running"        # 驱动 Agent 执行，逐条投递事件
    FINALIZING = "finalizing"  # 清理 Agent、通知 subagent、调度 idle 压缩
    COMPLETED = "completed"    # 正常完成（终态）
    CANCELLED = "cancelled"    # 被用户取消（终态）
    ERROR = "error"            # 执行异常（终态）


@dataclass
class Turn:
    """一个完整的用户交互周期（从收到消息到响应完成）。

    Turn 是贯穿整个处理流程的状态容器：
    - execute() 入口设置 turn_id / command / command_name
    - 各状态函数读取上游写入的字段、写入自己的产出给下游
    - 事件从状态转移中产生，统一带上 turn.turn_id
    """
    # ── 身份（execute 入口创建时设置，不可变）──
    turn_id: str                 # 本 Turn 唯一标识，所有事件都盖这个戳
    inbound: BusMessage          # 触发本 Turn 的用户消息
    session_id: str              # 所属会话

    # ── 当前状态（状态机读写）──
    status: TurnStatus = TurnStatus.COMMAND  # 状态机从 COMPACTING 起步

    # ── 指令匹配结果（execute 入口设置，命中指令时非 None）──
    command: "CommandDef | None" = None      # 命中的指令定义（含 system / persist_input）
    command_name: str | None = None          # 指令名（如 "/compact"），PIPELINE_END 会带上

    # ── 压缩决策（_compact 写入，_build 读取）──
    need_compact: bool = False   # True 表示 _build 里要先做关键路径压缩

    # ── Agent 执行上下文（_build 写入，_run 读取）──
    agent: "ReActAgent | None" = None        # 创建的 Agent 实例，None 表示未进入执行
    messages: list = field(default_factory=list)          # 发给 LLM 的消息列表
    runtime_context: dict = field(default_factory=dict)   # 工具共享的运行时上下文
    final_content: str = ""                  # 最后一条完整 assistant 回复（task 工具用）
    subagent_status: str = "completed"       # subagent 完成态：completed/cancelled/error

    # ── 事件序列（供回放/调试）──
    events: list = field(default_factory=list)  # 本 Turn 产生的所有 StepEvent


# ═══════════════════════════════════════════════════════════════════
# TurnExecutor
# ═══════════════════════════════════════════════════════════════════

class TurnExecutor:
    """单个 Turn 的完整执行：状态机驱动。

    AgentLoop 负责消费循环和并发控制，
    TurnExecutor 负责消息进来后的全部处理逻辑。
    """

    # 需要持久化的事件类型
    _PERSISTENT_CLASSES: tuple[type, ...] = (
        AssistantMessageCompleteEvent,
        ToolResultEvent,
        UserMessageEvent,
    )

    def __init__(self, loop: "AgentLoop") -> None:
        self._loop = loop

    # ─── 驱动入口 ────────────────────────────────────────────

    async def execute(self, inbound: BusMessage) -> None:
        """单条消息的处理入口——Turn 的编排中枢。

        execute() 只管"边界 + 并发"，不管业务逻辑：
        - PIPELINE_START / PIPELINE_END 边界（try/finally 保证成对）
        - 系统级指令锁外短路（/cancel 不能等锁）
        - per-session lock 管理
        - 状态机驱动循环

        指令匹配、用户消息存储、普通指令执行都在 COMMAND 状态里做。
        """
        loop = self._loop
        turn = Turn(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            inbound=inbound,
            session_id=self._session_id_of(inbound),
        )
        await self._emit_step(turn, StepPhase.PIPELINE_START)

        try:
            # ── 系统级指令（/cancel）：锁外立即执行，短路 ──
            # 必须锁外：用户点停止时不能被 session lock 阻塞。
            # match_any 只查不执行，系统级 handler 在这里执行。
            cmd_def = (
                loop.command_manager.match_any(turn)
                if loop.command_manager
                else None
            )
            if cmd_def is not None and cmd_def.system:
                turn.command = cmd_def
                turn.command_name = cmd_def.command
                await self._emit_step(
                    turn, StepPhase.COMMAND_MATCHED, command_name=cmd_def.command
                )
                await loop.command_manager.try_dispatch_system(turn)
                return  # 短路：PIPELINE_END 在 finally 发

            # ── 后续进 per-session 锁 ──
            current_task = asyncio.current_task()
            loop._session_tasks[turn.session_id] = current_task
            lock = loop._session_locks.setdefault(turn.session_id, asyncio.Lock())
            try:
                async with lock:
                    # 状态机：COMMAND → COMPACTING → BUILDING → RUNNING → FINALIZING
                    while turn.status not in (
                        TurnStatus.COMPLETED,
                        TurnStatus.CANCELLED,
                        TurnStatus.ERROR,
                    ):
                        turn.status = await self._advance(turn)
            except Exception:
                logger.exception(
                    f"[turn-executor] 状态机异常 session={turn.session_id} "
                    f"status={turn.status}"
                )
                turn.status = TurnStatus.ERROR
            finally:
                if turn.agent is not None:
                    await self._finalize(turn)
                if loop._session_tasks.get(turn.session_id) is current_task:
                    loop._session_tasks.pop(turn.session_id, None)
        finally:
            await self._emit_step(
                turn, StepPhase.PIPELINE_END,
                success=turn.status == TurnStatus.COMPLETED,
                reason="error" if turn.status == TurnStatus.ERROR else "",
                command_name=turn.command_name,
            )

    async def _advance(self, turn: Turn) -> TurnStatus:
        """状态转移：根据当前状态调对应处理函数，返回下一个状态。"""
        match turn.status:
            case TurnStatus.COMMAND:
                return await self._command(turn)    # → COMPACTING 或 COMPLETED
            case TurnStatus.COMPACTING:
                return await self._compact(turn)     # → BUILDING
            case TurnStatus.BUILDING:
                return await self._build(turn)      # → RUNNING（或 COMPLETED 鉴权失败）
            case TurnStatus.RUNNING:
                return await self._run(turn)         # → FINALIZING/CANCELLED/ERROR
            case TurnStatus.FINALIZING:
                return TurnStatus.COMPLETED
            case _:
                return turn.status

    # ─── 状态处理函数 ────────────────────────────────────────

    async def _command(self, turn: Turn) -> TurnStatus:
        """[状态 0] 匹配指令 + 存用户消息 + 执行 handler + 路由。

        这是状态机的第一个状态，所有非系统级消息都从这里起步。三种结局：
        - 未命中指令（普通消息）：存 user_message → COMPACTING
        - 命中普通指令 → 存 user_message → 执行 handler
          - Handled/SendMessage → COMPLETED（短路，不跑 Agent）
          - RewritePrompt/Passthrough → COMPACTING（继续跑 Agent）
        - command_manager 为 None → 当普通消息处理

        存储时机：在指令执行之前存，保证 DB 顺序是 user_message 在 Agent 事件之前。
        /cancel 等系统级指令不会进这个状态（execute 入口锁外短路了）。
        """
        loop = self._loop
        inbound = turn.inbound
        session_id = turn.session_id
        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []
        agent_id = (inbound.metadata or {}).get("agent_id", "") or "default"

        # ── 1. 匹配指令（不执行 handler，只判断是否命中）──
        cmd_def = (
            loop.command_manager.match_any(turn)
            if loop.command_manager
            else None
        )
        if cmd_def is not None:
            turn.command = cmd_def
            turn.command_name = cmd_def.command
            await self._emit_step(
                turn, StepPhase.COMMAND_MATCHED, command_name=cmd_def.command
            )

        # ── 2. 存用户消息（persist_input 决定存不存）──
        # 无命令 → 普通消息要存；有命令 → 看 persist_input
        should_persist = (cmd_def is None) or cmd_def.persist_input
        if should_persist and session_id and content:
            stored_content = normalize_stored_user_content(content)
            user_event_id = uuid.uuid4().hex[:16]
            user_metadata = {"hide": False, "agent_id": agent_id}
            user_data: dict = {"content": stored_content, "metadata": user_metadata}
            if attachments:
                user_data["attachments"] = attachments
            await loop.session_manager.save_message(
                session_id, "user_message", user_data,
                event_id=user_event_id,
                turn_id=turn.turn_id,
            )
            # echo 回前端：让用户立即看到自己发的消息
            echo = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data={
                    "type": "user_message",
                    "event_id": user_event_id,
                    "turn_id": turn.turn_id,
                    "data": {**inbound.data, "event_id": user_event_id},
                },
                metadata=inbound.metadata,
            )
            await loop.bus.publish_outbound(echo)

        # ── 3. 未命中指令 → 普通消息，继续状态机 ──
        if cmd_def is None:
            return TurnStatus.COMPACTING

        # ── 4. 命中普通指令 → 执行 handler，按返回值路由 ──
        # RewritePrompt：改写 prompt，继续跑 Agent
        # Passthrough：不拦截，继续跑 Agent
        # Handled：handler 自己处理完了，短路
        # SendMessage：推消息给前端，短路
        result = await loop.command_manager.try_dispatch(turn)
        if result is None:
            return TurnStatus.COMPACTING  # 不应该发生，安全默认

        match result:
            case RewritePrompt(content=prompt_content):
                # 改写发给 LLM 的 prompt（如 skill 展开），DB 存原始 content
                inbound_data = inbound.data
                if not isinstance(inbound_data.get("metadata"), dict):
                    inbound_data["metadata"] = {}
                inbound_data["metadata"]["prompt_override"] = prompt_content
                return TurnStatus.COMPACTING    # 继续跑 Agent
            case SendMessage(content=msg, level=level):
                await self._send_command_message(
                    session_id, inbound.from_channel, msg, level
                )
                return TurnStatus.COMPLETED       # 短路
            case Handled():
                return TurnStatus.COMPLETED        # 短路
            case Passthrough():
                return TurnStatus.COMPACTING       # 继续跑 Agent
            case _:
                return TurnStatus.COMPACTING

    async def _compact(self, turn: Turn) -> TurnStatus:
        """[状态 1/4] 判断是否需要压缩上下文。

        只做判断，不真正压缩——把结论写进 turn.need_compact，
        真正的压缩在 _build 里执行（因为压缩需要 config，_build 才加载 per-agent config）。

        只对 user_message 判断（其它类型消息直接进 BUILDING）。
        should_compact 看历史 token 是否超过阈值。异常不阻断，继续往下走。
        """
        loop = self._loop
        inbound = turn.inbound
        # 非用户消息不触发压缩判断
        if inbound.type != "user_message":
            return TurnStatus.BUILDING
        session_id = turn.session_id
        if not session_id:
            return TurnStatus.BUILDING

        try:
            config = self._load_current_config()
            need = await loop.compact_manager.should_compact(
                session_id,
                inbound.from_channel,
                config,
                threshold=getattr(config.context, "compact_threshold", 0.7),
            )
            if need:
                turn.need_compact = True  # 传给 _build，让它先压缩再构建消息
                logger.info(f"[turn-executor] 需要关键路径压缩 session={session_id}")
        except Exception:
            # 压缩判断失败不应阻断对话，记日志继续
            logger.exception(
                f"[turn-executor] should_compact 异常 session={session_id}"
            )

        return TurnStatus.BUILDING

    async def _build(self, turn: Turn) -> TurnStatus:
        """[状态 2/4] 鉴权 + 压缩 + 构建消息 + 创建 Agent + 组装 runtime_context。

        这一步做完 Agent 就准备好了，下一步 _run 直接驱动它。
        鉴权失败会直接返回 COMPLETED（turn.agent 保持 None，不会进 _finalize 清理）。
        """
        loop = self._loop
        inbound = turn.inbound
        session_id = turn.session_id
        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []

        # ── 鉴权 1：session 必须存在 ──
        session = await loop.session_manager.get_session(session_id)
        if session is None:
            logger.warning(
                f"[turn-executor] session 不存在，拒绝执行: session={session_id}"
            )
            return TurnStatus.COMPLETED  # 短路，不创建 Agent
        # ── 鉴权 2：session 的 channel 必须和消息来源一致（防串台）──
        if session["channel_id"] != inbound.from_channel:
            logger.warning(
                f"[turn-executor] session 与 channel 不匹配: "
                f"session={session_id} (channel={session['channel_id']}), "
                f"消息来自 {inbound.from_channel}"
            )
            return TurnStatus.COMPLETED

        # ── 加载 per-agent 配置（不同 agent 可用不同 LLM）──
        agent_id = (inbound.metadata or {}).get("agent_id", "") or "default"
        agent_profile = None
        if loop.agent_manager is not None:
            agent_profile = loop.agent_manager.load(agent_id)

        # ── 并发防御：理论上 session lock 已保证串行，这里是兜底 ──
        # 若发现同 session 已有 Agent 在跑，说明锁逻辑有 bug，强制取消旧的
        if session_id in loop._active_agents:
            existing = loop._active_agents[session_id]
            logger.error(
                f"[turn-executor] session lock 未能防止并发: "
                f"session={session_id}, existing_agent={existing!r}"
            )
            existing.cancel_nowait()

        # ── 关键路径压缩（_compact 判定需要时才做）──
        config = self._load_current_config()
        if agent_profile is not None:
            # per-agent 只覆盖 llm，不覆盖 workspace（agent 的家目录 ≠ 对话 cwd）
            config = copy.deepcopy(config)
            config.llm = agent_profile.llm
        if turn.need_compact:
            try:
                silent = getattr(config.context, "silent", True)
                await loop.compact_manager.compact(
                    session_id,
                    inbound.from_channel,
                    config=config,
                    silent=silent,
                    trigger="auto",
                )
            except Exception:
                logger.exception(
                    f"[turn-executor] 关键路径压缩异常 session={session_id}"
                )

        # ── 构建发给 LLM 的消息 ──
        workspace = session.get("workspace", "") or config.workspace or os.getcwd()
        # prompt_override 来自 RewritePrompt 指令：发给 LLM 用改写后的，DB 存原始
        prompt_override = (inbound.data.get("metadata") or {}).get("prompt_override")
        llm_content = prompt_override if prompt_override else content
        messages, hook_config = await self._build_messages(
            session_id,
            llm_content,
            attachments,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=workspace,
            agent_dir=(agent_profile.agent_dir if agent_profile else ""),
            turn_id=turn.turn_id,
        )

        # ── 创建 Agent 并注册到 _active_agents（/cancel 时通过它取消）──
        assert loop.agent_manager is not None, "agent_manager must be provided"
        agent = loop.agent_manager.create_agent(
            profile=agent_profile,
            config=hook_config,
            channel_manager=loop.channel_manager,
            tool_registry=loop.tool_registry,
            tracer=loop.tracer,
            channel_id=inbound.from_channel,
            session_id=session_id,
            hook_manager=loop.core_hook_manager,
        )
        turn.agent = agent  # 标记：已创建 Agent，execute finally 会走 _finalize
        loop._active_agents[session_id] = agent
        # 广播运行态：客户端显示"运行中"
        await self._publish_session_status_async(session_id, "running")

        # ── 组装 runtime_context（工具执行时的共享数据）──
        turn.runtime_context = {
            "session_id": session_id,
            "channel_id": inbound.from_channel,
            "event_loop": loop._event_loop,
            "session_manager": loop.session_manager,
            "bus": loop.bus,
            "agent_loop": loop,
            "llm_config": hook_config.llm,
            "agent_profile": agent_profile,
            "workspace": WorkspaceAccessor(
                session_id=session_id,
                session_manager=loop.session_manager,
                event_loop=loop._event_loop,  # type: ignore[arg-type]
                fallback_cwd=workspace,
            ),
            "trace_name": f"session:{session_id}",
            "trace_tags": [inbound.from_channel or "unknown"],
            "trace_metadata": {
                "session_id": session_id,
                "channel_id": inbound.from_channel,
                "workspace": workspace,
            },
            "turn_id": turn.turn_id,
        }

        # ── before_agent_run hook：插件可注入对话上下文/系统身份 ──
        if loop.hook_manager is not None:
            from ftre.plugin import BEFORE_AGENT_RUN, AgentRunContext

            ctx = AgentRunContext(
                session_id=session_id,
                channel_id=inbound.from_channel,
                messages=messages,
                config=hook_config,
                agent_profile=agent_profile,
                agent_tool_registry=agent.tool_registry,
            )
            ctx = await loop.hook_manager.trigger(BEFORE_AGENT_RUN, ctx)
            turn.messages = ctx.messages  # hook 可能改写了 messages
        else:
            turn.messages = messages

        return TurnStatus.RUNNING

    async def _run(self, turn: Turn) -> TurnStatus:
        """[状态 3/4] 驱动 Agent 执行，逐条投递事件。

        流程：TURN_START → 遍历 agent.run() 产出的事件 → TURN_END。
        三种结局：
        - 正常跑完 → TURN_END（reason 从 agent.state 取）→ FINALIZING
        - 被 cancel → TURN_END(cancelled) → CANCELLED
        - 抛异常   → TURN_END(error) → ERROR

        无论哪种结局都会发 TURN_END，保证客户端和 DB 里 Turn 有完整边界。
        """
        agent = turn.agent
        turn.subagent_status = "completed"
        turn.final_content = ""

        try:
            # TURN_START：Agent 执行开始，客户端据此显示流式区域
            await self._emit_step(turn, StepPhase.TURN_START, start_trigger="user")

            # ── 遍历 Agent 产出的事件流 ──
            async for event in agent.run(
                turn.messages, runtime_context=turn.runtime_context
            ):
                # 记录最后一条完整回复（task 工具作为返回值用）
                if isinstance(event, AssistantMessageCompleteEvent):
                    turn.final_content = event.content or ""

                # 持久化类事件（assistant/tool_result/user_message）：入库 + 推前端
                # 其它事件（如流式片段）：只推前端不入库
                if isinstance(event, self._PERSISTENT_CLASSES):
                    await self.publish_agent_event(turn.session_id, turn.inbound, event)
                else:
                    await self._dispatch_agent_event(turn.inbound, event)

                # 每次完整回复后检查是否要调度后台 idle 压缩（自带去重）
                if (
                    isinstance(event, AssistantMessageCompleteEvent)
                    and event.metadata.get("usage")
                    and turn.inbound.from_channel != SUBAGENT_CHANNEL_ID
                ):
                    try:
                        _cfg = self._load_current_config()
                        await self._loop.compact_manager.maybe_schedule_idle_compact(
                            turn.session_id, turn.inbound.from_channel, _cfg
                        )
                    except Exception:
                        logger.debug(
                            "[turn-executor] 调度 usage 压缩失败", exc_info=True
                        )

            # ── 正常结束：TURN_END 的字段从 agent.state 读 ──
            _is_error = agent.state.done_reason == DoneReason.ERROR
            await self._emit_step(
                turn,
                StepPhase.TURN_END,
                success=(agent.state.done_reason == DoneReason.COMPLETED),
                reason=agent.state.done_reason or DoneReason.ERROR,
                iterations=agent.state.iteration,
                token_usage=dict(agent.state.token_usage),
                error_message=agent.state.error if _is_error else None,
                error_code=agent.state.error_code if _is_error else None,
            )
            return TurnStatus.FINALIZING

        except asyncio.CancelledError:
            # 被 /cancel 触发的 task.cancel() 中断（在 LLM stream 的 await 处抛出）
            turn.subagent_status = "cancelled"
            logger.info(f"[turn-executor] Agent 被 cancel 中断 session={turn.session_id}")
            # 仍发 TURN_END，让客户端知道已停止、历史回放有完整边界
            await self._emit_step(
                turn, StepPhase.TURN_END, success=False, reason=DoneReason.CANCELLED
            )
            return TurnStatus.CANCELLED
        except Exception:
            # 未预期异常
            turn.subagent_status = "error"
            logger.exception(
                f"[turn-executor] _run 异常 (session={turn.session_id})"
            )
            await self._emit_step(
                turn,
                StepPhase.TURN_END,
                success=False,
                reason=DoneReason.ERROR,
                error_message="Agent 执行异常",
                error_code="unknown",
            )
            return TurnStatus.ERROR

    async def _finalize(self, turn: Turn) -> TurnStatus:
        """[状态 4/4] 收尾：清理 Agent 注册、通知 subagent、调度 idle 压缩。

        在 execute() 的 finally 里统一调用（无论正常/取消/异常都会走），
        所以它是 Turn 的唯一收尾出口，必须幂等且不抛异常。
        """
        loop = self._loop
        session_id = turn.session_id

        # ── 摘除 _active_agents（仅当还是自己创建的那个）──
        # 若已被后来的 Agent 顶替，不清理（避免误删别人的）
        if loop._active_agents.get(session_id) is turn.agent:
            loop._active_agents.pop(session_id, None)
            should_emit_idle = True
        else:
            should_emit_idle = False

        # ── subagent 场景：唤醒等待结果的父 task（task 工具）──
        # finally 覆盖正常/取消/异常，是父 task 被唤醒的唯一出口
        if turn.inbound.from_channel == SUBAGENT_CHANNEL_ID:
            future = loop._subagent_done_futures.pop(session_id, None)
            if future is not None and not future.done():
                future.set_result(
                    {
                        "session_id": session_id,
                        "channel_id": turn.inbound.from_channel,
                        "status": turn.subagent_status,
                        "final_content": turn.final_content,
                    }
                )

        # ── 广播 idle：客户端恢复空闲态 ──
        if should_emit_idle:
            await self._publish_session_status_async(session_id, "idle")

        # ── 本轮结束后调度后台 idle 压缩（非 subagent）──
        if turn.inbound.from_channel != SUBAGENT_CHANNEL_ID:
            try:
                _cfg = self._load_current_config()
                await loop.compact_manager.maybe_schedule_idle_compact(
                    session_id, turn.inbound.from_channel, _cfg
                )
            except Exception:
                logger.debug("[turn-executor] 调度 idle 压缩失败", exc_info=True)

        return TurnStatus.COMPLETED

    # ─── 事件发布 ──────────────────────────────────────────

    async def _emit_step(self, turn: Turn, phase: StepPhase, **kwargs) -> None:
        """构造 StepEvent 并发布（入库 + 推前端）。

        所有 Turn 边界事件（PIPELINE_START/END、COMMAND_MATCHED、
        TURN_START/END）都走这里，统一盖上 turn.turn_id。
        """
        event = StepEvent(phase=phase, **kwargs)
        # StepEvent 是 frozen-ish，用 object.__setattr__ 盖 turn_id
        object.__setattr__(event, "turn_id", turn.turn_id)
        await self.publish_agent_event(turn.session_id, turn.inbound, event)
        turn.events.append(event)  # 记入 Turn 的事件序列（供回放/调试）

    async def publish_agent_event(
        self, session_id: str, inbound: BusMessage, event
    ) -> None:
        """存储 agent event 到 DB + 派发到前端（两件事一起做）。"""
        loop = self._loop
        # 1. 入库（历史回放用）
        await loop.session_manager.save_message(
            session_id,
            event.type.value,
            event._data_dict(),
            event_id=event.event_id,
            turn_id=event.turn_id,
            timestamp=event.timestamp,
        )
        # 2. 推前端（实时显示）
        await self._dispatch_agent_event(inbound, event)

    async def _dispatch_agent_event(self, inbound: BusMessage, event) -> None:
        """只派发 agent event 到前端，不入库（用于流式片段等临时事件）。"""
        loop = self._loop
        await loop.bus.publish_outbound(
            BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data=event.to_dict(),
            )
        )

    async def _publish_session_status_async(
        self, session_id: str, status: str
    ) -> None:
        """广播 session 运行态变化（idle/running/compacting）到全局频道。

        客户端据此更新 UI 状态（如显示"运行中"/"空闲"）。
        """
        loop = self._loop
        evt = BusMessage(
            type="global_event",
            from_channel=GLOBAL_CHANNEL,
            to_channel=GLOBAL_CHANNEL,
            from_session=GLOBAL_SESSION,
            to_session=GLOBAL_SESSION,
            data={
                "type": "session_status",
                "data": {"session_id": session_id, "status": status},
            },
        )
        await loop.bus.publish_outbound(evt)

    async def _send_command_message(
        self, session_id: str, channel_id: str, content: str, level: str = "info"
    ) -> None:
        """指令 handler 返回 SendMessage 时，推一条 info/error 消息给前端。

        用于 /help 这类只需给用户看一段文字、不跑 Agent 的指令。
        """
        loop = self._loop
        evt = BusMessage(
            type="session_event",
            from_channel=channel_id,
            to_channel=channel_id,
            from_session=session_id,
            to_session=session_id,
            data={
                "type": "command_message",
                "data": {"content": content, "level": level},
            },
        )
        await loop.bus.publish_outbound(evt)

    # ─── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _session_id_of(inbound: BusMessage) -> str:
        """从 BusMessage 提取 session_id（data 优先，回退到 from_session）。"""
        return inbound.data.get("session_id", "") or inbound.from_session

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置（测试注入优先，否则从磁盘加载）。"""
        loop = self._loop
        if loop._injected_config is not None:
            return loop._injected_config
        return load_config()

    async def _build_messages(
        self,
        session_id: str,
        content: str,
        attachments: list[dict],
        config: AgentConfig,
        *,
        inbound_data: dict | None = None,
        channel_id: str = "",
        workspace: str = "",
        agent_dir: str = "",
        turn_id: str,
    ) -> tuple[list[dict], AgentConfig]:
        """构建发给 LLM 的消息列表，触发 before_messages_build hook。

        关键点：用户消息已在 _command 状态提前存到 DB，这里从 DB 读历史
        （get_messages_by_session）时已经包含它。所以【不能再 append】当前
        用户输入，否则 LLM 会收到两份重复消息。

        prompt_override（RewritePrompt 指令改写）通过覆盖最后一条 user 消息
        的 content 实现——DB 存原始，发给 LLM 用改写后的。
        """
        loop = self._loop
        # 从 DB 读全部历史（已含本轮用户消息，因为 _command 先存了）
        events = await loop.session_manager.get_messages_by_session(session_id)

        # 触发 before_messages_build hook（插件做孤立事件清理、裁剪、标题生成等）
        hook_config = copy.deepcopy(config)
        if loop.hook_manager is not None:
            from ftre.plugin import BEFORE_MESSAGES_BUILD, MessagesBuildContext

            ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=channel_id,
                inbound_data=inbound_data or {},
                workspace=workspace,
                turn_id=turn_id,
                agent_dir=agent_dir,
                config=hook_config,
                events=events,
            )
            ctx = await loop.hook_manager.trigger(BEFORE_MESSAGES_BUILD, ctx)
            hook_config = ctx.config
            events = ctx.events

        # 当前用户输入转成 OpenAI content 格式（文字 + 图片）
        user_content = build_user_content(
            content,
            attachments,
            include_images=hook_config.llm.vision,
        )

        if events:
            from ftre.session.converter import to_openai

            # 历史事件转 OpenAI messages（已含本轮 user_message）
            history = to_openai(
                events,
                config={"llm": {"vision": hook_config.llm.vision}},
            )
            # 用户消息已在 _command 中提前持久化到 DB，to_openai 已包含它。
            # 不再 append（会导致 LLM 收到两份重复消息）。
            # 如果有 prompt_override（指令重写），替换最后一条 user 消息的内容。
            if user_content:
                replaced = False
                # 从尾部找最后一条 user 消息，覆盖其 content
                for i in range(len(history) - 1, -1, -1):
                    if history[i].get("role") == "user":
                        history[i] = {**history[i], "content": user_content}
                        replaced = True
                        break
                # 兜底：DB 里居然没有 user 消息（异常情况），append 一条
                if not replaced:
                    history.append({"role": "user", "content": user_content})
            return history, hook_config

        # 无历史（首条消息）：直接用当前输入
        return [{"role": "user", "content": user_content}], hook_config
