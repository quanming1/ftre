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

from ftre_agent_core.agent import ReActAgent, RunStatus
from ftre_agent_core.event import (
    EventBase,
    ReplyStartEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    CustomEvent,
    ToolResultEndEvent,
    UserMessageEvent,
)
from ftre_agent_core.message import Msg, from_openai_message

from ftre.bus import BusMessage, GLOBAL_CHANNEL, GLOBAL_SESSION
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID
from ftre.config import AgentConfig, load_config
from ftre.session.message.multimodal import build_user_content, normalize_stored_user_content
from ftre.tools._workspace import WorkspaceAccessor, ensure_workspace_ext_dir

from .event_hub import AgentEventHub

from ftre.command.types import (
    Handled,
    Passthrough,
    ResumeAgent,
    RewritePrompt,
    SendMessage,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_manager import AgentProfile
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
    - 事件从状态转移中产生，reply_id 关联到 turn.turn_id
    """
    # ── 身份（execute 入口创建时设置，不可变）──
    turn_id: str                 # 本 Turn 唯一标识，作为 reply_id 关联事件
    inbound: BusMessage          # 触发本 Turn 的用户消息
    session_id: str              # 所属会话

    # ── 当前状态（状态机读写）──
    status: TurnStatus = TurnStatus.COMMAND  # 状态机从 COMPACTING 起步

    # ── 指令匹配结果（execute 入口设置，命中指令时非 None）──
    command: "CommandDef | None" = None      # 命中的指令定义（含 system / persist_input）
    command_name: str | None = None          # 指令名（如 "/compact"），PIPELINE_END 会带上

    # ── 压缩决策（_compact 写入，_build 读取）──
    need_compact: bool = False   # True 表示 _build 里要先做关键路径压缩
    user_message_id: str = ""    # 本轮已持久化 UserMsg；关键路径压缩从它开始保留 tail

    # ── Agent 执行上下文（_build 写入，_run 读取）──
    agent_profile: "AgentProfile | None" = None  # 本轮选定的 Agent 私有配置
    config: "AgentConfig | None" = None          # 本轮实际使用的有效配置快照
    agent: "ReActAgent | None" = None        # 创建的 Agent 实例，None 表示未进入执行
    messages: list = field(default_factory=list)          # 发给 LLM 的消息列表
    runtime_context: dict = field(default_factory=dict)   # 工具共享的运行时上下文
    final_content: str = ""                  # 最后一条完整 assistant 回复（task 工具用）
    subagent_status: str = "completed"       # subagent 完成态：completed/cancelled/error

    # ── 权限确认恢复（/allow、/deny 指令触发时非 None）──
    # 非 None 表示本 Turn 是恢复请求：跳过普通消息构建，
    # 注入历史 context 到新 agent，run() 时传入此事件而非 messages。
    confirm_event: object | None = None

    # ── 事件序列（供回放/调试）──
    events: list = field(default_factory=list)  # 本 Turn 产生的所有 CustomEvent


# ═══════════════════════════════════════════════════════════════════
# TurnExecutor
# ═══════════════════════════════════════════════════════════════════

class TurnExecutor:
    """单个 Turn 的完整执行：状态机驱动。

    AgentLoop 负责消费循环和并发控制，
    TurnExecutor 负责消息进来后的全部处理逻辑。
    """

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
        session_id = self._session_id_of(inbound)

        # 压缩期间不接受新用户输入。必须在 PIPELINE_START、UserMessageEvent
        # 和 UserMsg 入库之前丢弃，避免客户端/磁盘出现没有后续回复的半条消息。
        if (
            inbound.type == "user_message"
            and session_id
            and loop.compact_manager.is_compacting(session_id)
        ):
            frame_id = inbound.metadata.frame_id
            logger.warning(
                "[compact] session=%s 正在压缩，丢弃新消息 frame_id=%s",
                session_id,
                frame_id or "-",
            )
            return

        turn = Turn(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            inbound=inbound,
            session_id=session_id,
        )
        await self._emit_step(turn, "PIPELINE_START")

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
                    turn, "COMMAND_MATCHED", command_name=cmd_def.command
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
                turn, "PIPELINE_END",
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
        - 未命中指令（普通消息）：存 UserMsg → COMPACTING
        - 命中普通指令 → 存 UserMsg → 执行 handler
          - Handled/SendMessage → COMPLETED（短路，不跑 Agent）
          - RewritePrompt/Passthrough → COMPACTING（继续跑 Agent）
        - command_manager 为 None → 当普通消息处理

        存储时机：在指令执行之前存，保证 DB 中 UserMsg 位于本轮 AssistantMsg 之前。
        /cancel 等系统级指令不会进这个状态（execute 入口锁外短路了）。
        """
        loop = self._loop
        inbound = turn.inbound
        session_id = turn.session_id

        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []
        agent_id = inbound.metadata.agent_id or "default"

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
                turn, "COMMAND_MATCHED", command_name=cmd_def.command
            )

        # ── 2. 存用户消息（persist_input 决定存不存）──
        # 无命令 → 普通消息要存；有命令 → 看 persist_input
        should_persist = (cmd_def is None) or cmd_def.persist_input
        if should_persist and session_id and content:
            stored_content = normalize_stored_user_content(content)
            user_metadata = {"hide": False, "agent_id": agent_id}
            persisted_content = build_user_content(
                stored_content,
                attachments,
                include_images=True,
            )
            user_event = UserMessageEvent(
                reply_id=turn.turn_id,
                content=from_openai_message(
                    {"role": "user", "content": persisted_content}
                ),
                message_metadata=user_metadata,
                data={**inbound.data},
            )
            user_event.data["id"] = user_event.id
            result = await loop.emit_session_event(
                session_id,
                inbound.from_channel,
                user_event,
                metadata=inbound.metadata,
            )
            turn.user_message_id = result.persisted_messages[0].id

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
            case ResumeAgent(events=events):
                if not events:
                    return TurnStatus.COMPLETED
                # 批量确认必须先全部落盘。核心层随后从持久化上下文读取整批
                # ALLOWED/FINISHED 状态，最后一个事件只负责触发恢复。
                for event in events:
                    await loop.emit_session_event(
                        session_id,
                        inbound.from_channel,
                        event,
                        metadata=inbound.metadata,
                    )
                turn.confirm_event = events[-1]
                return TurnStatus.BUILDING
            case _:
                return TurnStatus.COMPACTING

    async def _compact(self, turn: Turn) -> TurnStatus:
        """[状态 1/4] 判断是否需要压缩上下文。

        只做判断，不真正压缩——把结论写进 turn.need_compact，
        真正的压缩在 _build 里执行。判断和执行必须共用本 Turn 的有效配置快照，
        否则 coder 等私有 Agent 会被 default Agent 的 context_window 错误判定。

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
            config, _ = await self._resolve_turn_config(turn)
            need = await loop.compact_manager.should_compact(
                session_id,
                inbound.from_channel,
                config,
                threshold=getattr(config.context, "compact_threshold", 0.8),
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

        # ── 取得本 Turn 已解析的有效配置（不同 agent 可用不同 LLM）──
        # _compact 通常已经建立快照；非 user_message 等特殊路径在这里兜底建立。
        config, agent_profile = await self._resolve_turn_config(turn)

        # ── 并发防御：理论上 session lock 已保证串行，这里是兜底 ──
        # 若发现同 session 已有 Agent 在跑，说明锁逻辑有 bug，强制取消旧的
        if session_id in loop._active_agents:
            existing = loop._active_agents[session_id]
            logger.error(
                f"[turn-executor] session lock 未能防止并发: "
                f"session={session_id}, existing_agent={existing!r}"
            )
            existing.cancel_nowait()

        # ── 权限确认恢复分支：注入历史 Msg 到新 agent，跳过普通消息构建 ──
        if turn.confirm_event is not None:
            return await self._build_resume(
                turn, session, config, agent_profile
            )

        # ── 关键路径压缩（_compact 判定需要时才做）──
        if turn.need_compact:
            # 用户消息已经进入关键路径并正在等待摘要完成，先切换为压缩状态。
            loop._compacting_sessions.add(session_id)
            await self._publish_session_status_async(session_id, "compacting")
            try:
                await loop.compact_manager.compact(
                    session_id,
                    inbound.from_channel,
                    config=config,
                    trigger="auto",
                    preserve_from_message_id=turn.user_message_id,
                )
            except Exception:
                logger.exception(
                    f"[turn-executor] 关键路径压缩异常 session={session_id}"
                )
            finally:
                loop._compacting_sessions.discard(session_id)

        # ── 构建发给 LLM 的消息 ──
        workspace = session.get("workspace", "") or config.workspace or os.getcwd()
        # 发送消息时确保当前工作区有 .ftre 扩展目录骨架（工作区级 skill / mcp.json 的落点）
        ensure_workspace_ext_dir(workspace)
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
            reply_id=turn.turn_id,
        )
        # 后续 idle compact 必须继续使用真正创建 Agent 时的配置，而不是重新读取
        # 此刻可能已经切换过的 default Agent 配置。
        turn.config = copy.deepcopy(hook_config)
        if agent_profile is not None:
            # create_agent() 也会以 profile.llm 为最终值；这里保持快照与真实
            # Agent 完全一致，避免 hook/test double 返回了另一套 llm。
            turn.config.llm = copy.deepcopy(agent_profile.llm)

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
            "reply_id": turn.turn_id,
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
                workspace=workspace,
            )
            ctx = await loop.hook_manager.trigger(BEFORE_AGENT_RUN, ctx)
            turn.messages = ctx.messages  # hook 可能改写了 messages
        else:
            turn.messages = messages

        return TurnStatus.RUNNING

    async def _build_resume(
        self, turn: Turn, session: dict, config, agent_profile
    ) -> TurnStatus:
        """[状态 2/4 · 恢复] 权限确认恢复专用构建。

        与普通 _build 的差异：
        - 不写 UserMsg、不做 compact；
        - 直接把持久化历史 Msg（含 ASKING 的 tool_call）注入 AgentState.context，
          而非转成 provider dict——ToolCallState 只存活在 typed Msg 里；
        - 注入默认权限规则的 permission_context；
        - runtime_context.reply_id 用确认事件携带的原 reply_id，保证恢复产出的
          工具结果/后续事件聚合回原 assistant Msg。
        """
        from ftre_agent_core.agent import AgentState
        from ftre.session.message.converter import _as_msg

        loop = self._loop
        inbound = turn.inbound
        session_id = turn.session_id
        workspace = session.get("workspace", "") or config.workspace or os.getcwd()

        # 只读 LLM 有效上下文（最后一条 compact 摘要 + tail），避免把已经被摘要
        # 覆盖的完整 transcript 重新注入。保留 typed Msg 以维持 ToolCallState。
        records = await loop.session_manager.get_context_messages(session_id)
        hook_config = copy.deepcopy(config)
        if loop.hook_manager is not None:
            from ftre.plugin import BEFORE_MESSAGES_BUILD, MessagesBuildContext

            messages_ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=inbound.from_channel,
                inbound_data=inbound.data,
                workspace=workspace,
                reply_id=turn.confirm_event.reply_id,
                agent_dir=(agent_profile.agent_dir if agent_profile else ""),
                config=hook_config,
                messages=records,
            )
            messages_ctx = await loop.hook_manager.trigger(
                BEFORE_MESSAGES_BUILD, messages_ctx
            )
            hook_config = messages_ctx.config
            records = messages_ctx.messages
        context_msgs = [_as_msg(r) for r in records]

        # 复用默认权限规则，注入历史 context
        state = loop.agent_manager._default_agent_state()
        state.context = context_msgs

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
            state=state,
        )

        # 恢复路径同样必须触发 BEFORE_AGENT_RUN：私有 MCP 工具在这里按需注册，
        # Skill/MCP/Plan 等插件也会扩展 system prompt。typed context 继续作为
        # 权限状态事实源，hook 只修改 provider 视图与 agent 的 system prompt。
        if loop.hook_manager is not None:
            from ftre.plugin import BEFORE_AGENT_RUN, AgentRunContext
            from ftre_agent_core.message_context import MessageContext

            hook_messages = MessageContext.get_messages(
                state.context, agent.system_prompt
            )
            ctx = AgentRunContext(
                session_id=session_id,
                channel_id=inbound.from_channel,
                messages=hook_messages,
                config=hook_config,
                agent_profile=agent_profile,
                agent_tool_registry=agent.tool_registry,
                workspace=workspace,
            )
            ctx = await loop.hook_manager.trigger(BEFORE_AGENT_RUN, ctx)
            system_parts = [
                str(message.get("content") or "")
                for message in ctx.messages
                if isinstance(message, dict) and message.get("role") == "system"
            ]
            if system_parts:
                agent.system_prompt = "\n\n".join(
                    part for part in system_parts if part
                )

        turn.agent = agent
        turn.config = copy.deepcopy(hook_config)
        loop._active_agents[session_id] = agent
        await self._publish_session_status_async(session_id, "running")

        reply_id = turn.confirm_event.reply_id
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
            "reply_id": reply_id,
        }
        return TurnStatus.RUNNING

    async def _run(self, turn: Turn) -> TurnStatus:
        """[状态 3/4] 驱动 Agent 执行，逐条投递事件。

        流程：TURN_START → 遍历 agent.run() 产出的事件 → TURN_END。
        三种结局：
        - 正常跑完 → TURN_END（reason 从 agent.run_state 取）→ FINALIZING
        - 被 cancel → TURN_END(cancelled) → CANCELLED
        - 抛异常   → TURN_END(error) → ERROR

        无论哪种结局都会发 TURN_END，保证客户端和 DB 里 Turn 有完整边界。
        """
        agent = turn.agent
        turn.subagent_status = "completed"
        turn.final_content = ""

        try:
            # TURN_START：Agent 执行开始，客户端据此显示流式区域
            await self._emit_step(turn, "TURN_START", start_trigger="user")

            # ── 遍历 Agent 产出的事件流 ──
            # 恢复请求传 UserConfirmResultEvent 驱动 core 从挂起继续；
            # 普通请求传消息列表。
            run_input = (
                turn.confirm_event if turn.confirm_event is not None else turn.messages
            )
            async for event in agent.run(
                run_input, runtime_context=turn.runtime_context
            ):
                # Event 只用于实时传输；在内存中聚合，REPLY_END 时才落一条 Msg。
                completed_message = await self.publish_agent_event(turn, event)
                if completed_message is not None:
                    turn.final_content = completed_message.get_text_content() or ""

                # 每次完整回复后检查是否要调度后台 idle 压缩（自带去重）
                if (
                    isinstance(event, ReplyEndEvent)
                    and turn.inbound.from_channel != SUBAGENT_CHANNEL_ID
                ):
                    try:
                        _cfg, _ = await self._resolve_turn_config(turn)
                        await self._loop.compact_manager.maybe_schedule_idle_compact(
                            turn.session_id, turn.inbound.from_channel, _cfg
                        )
                    except Exception:
                        logger.debug(
                            "[turn-executor] 调度 usage 压缩失败", exc_info=True
                        )

            # AgentState 只保存可持久化的消息上下文；一次 run 的结束原因、
            # 迭代次数、token 用量和错误信息都属于临时 RunState。
            run_state = agent.run_state

            # ── 权限挂起（PAUSED）──
            # 工具命中 ASK → run() 提前结束但未 finalize，done_reason 为 None。
            # 这不是错误也不是回复结束：ASKING 状态已随 Msg 落盘，不产 error TURN_END、
            # 不 finish open replies；发一条 success 的 TURN_END(reason=paused) 让客户端
            # 退出 busy，Turn 正常收尾、agent 实例销毁。用户确认后由新 Turn 恢复。
            if run_state.status == RunStatus.PAUSED:
                await self._emit_step(
                    turn,
                    "TURN_END",
                    success=True,
                    reason="paused",
                    iterations=run_state.iteration,
                    token_usage=dict(run_state.token_usage),
                )
                return TurnStatus.FINALIZING

            done_reason = run_state.done_reason or ReplyFinishedReason.ERROR
            _is_error = done_reason == ReplyFinishedReason.ERROR
            await self._emit_step(
                turn,
                "TURN_END",
                success=(done_reason == ReplyFinishedReason.COMPLETED),
                reason=str(done_reason),
                iterations=run_state.iteration,
                token_usage=dict(run_state.token_usage),
                error_message=run_state.error if _is_error else None,
                error_code=run_state.error_code if _is_error else None,
            )
            return TurnStatus.FINALIZING

        except asyncio.CancelledError:
            # 被 /cancel 触发的 task.cancel() 中断（在 LLM stream 的 await 处抛出）
            turn.subagent_status = "cancelled"
            logger.info(f"[turn-executor] Agent 被 cancel 中断 session={turn.session_id}")
            await self._persist_open_replies(
                turn, ReplyFinishedReason.INTERRUPTED
            )
            # 仍发 TURN_END，让实时客户端立即结束本轮 busy 状态
            await self._emit_step(
                turn, "TURN_END", success=False, reason=str(ReplyFinishedReason.INTERRUPTED)
            )
            return TurnStatus.CANCELLED
        except Exception:
            # 未预期异常
            turn.subagent_status = "error"
            logger.exception(
                f"[turn-executor] _run 异常 (session={turn.session_id})"
            )
            await self._persist_open_replies(
                turn,
                ReplyFinishedReason.ERROR,
                error={"message": "Agent 执行异常", "code": "unknown"},
            )
            await self._emit_step(
                turn,
                "TURN_END",
                success=False,
                reason=str(ReplyFinishedReason.ERROR),
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

        # ── 广播 agent 完成事件（AgentEventHub）──
        # 无条件 emit（不依赖通道推断）：task/team 只对 subagent session wait，
        # 非 subagent 完成时无人订阅，零开销；未来其它功能可订阅同一事件。
        try:
            loop.events.emit(
                session_id,
                AgentEventHub.AGENT_FINISHED,
                {
                    "session_id": session_id,
                    "channel_id": turn.inbound.from_channel,
                    "status": turn.subagent_status,
                    "final_content": turn.final_content,
                },
            )
        except Exception:
            logger.exception("[turn-executor] 广播 agent_finished 事件异常")

        # ── 广播 idle：客户端恢复空闲态 ──
        if should_emit_idle:
            await self._publish_session_status_async(session_id, "idle")

        # ── 本轮结束后调度后台 idle 压缩（非 subagent）──
        if turn.inbound.from_channel != SUBAGENT_CHANNEL_ID:
            try:
                _cfg, _ = await self._resolve_turn_config(turn)
                await loop.compact_manager.maybe_schedule_idle_compact(
                    session_id, turn.inbound.from_channel, _cfg
                )
            except Exception:
                logger.debug("[turn-executor] 调度 idle 压缩失败", exc_info=True)

        return TurnStatus.COMPLETED

    # ─── 事件发布 ──────────────────────────────────────────

    async def _emit_step(self, turn: Turn, phase: str, **kwargs) -> None:
        """构造 CustomEvent 并实时推送。

        所有 Turn 边界事件（PIPELINE_START/END、COMMAND_MATCHED、
        TURN_START/END）都走这里，用 CustomEvent 携带 phase 信息。
        reply_id 关联到 turn.turn_id（通过 metadata 传递，CustomEvent 无 reply_id 字段）。
        """
        event = CustomEvent(
            name=phase,
            value=kwargs,
            metadata={"reply_id": turn.turn_id},
        )
        await self.publish_agent_event(turn, event)
        turn.events.append(event)  # 记入 Turn 的事件序列（供回放/调试）

    async def publish_agent_event(
        self, turn: Turn, event
    ) -> Msg | None:
        """实时派发 Event，并在 Reply 生命周期内管理 Msg 持久化。

        - REPLY_START: 创建 AssistantMsg + save_message + 注册 ActiveReplyRegistry
        - 其他 Event: append_event + checkpoint（节流/立即）
        - REPLY_END: append_event + update_message + 注销 registry
        - CustomEvent(context_compact_done): 投影为 user/compact Msg 并落盘
        """
        result = await self._loop.emit_session_event(
            turn.session_id,
            turn.inbound.from_channel,
            event,
            metadata=turn.inbound.metadata,
        )
        return result.completed_message

    async def _persist_open_replies(
        self,
        turn: Turn,
        reason: ReplyFinishedReason,
        *,
        error: dict | None = None,
    ) -> None:
        """异常中断时更新已持久化的 open Msg，写入终态。"""
        for message in await self._loop.session_projection.finish_open(
            turn.session_id, reason, error=error
        ):
            turn.final_content = message.get_text_content() or turn.final_content

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

    async def _resolve_turn_config(
        self, turn: Turn
    ) -> tuple[AgentConfig, "AgentProfile | None"]:
        """取得并缓存本 Turn 真正使用的 Agent 配置。

        context_window、模型调用和回复结束后的 idle compact 必须来自同一份快照。
        不能在 compact 判断时只读全局/default 配置，再在 _build 阶段才覆盖
        per-agent LLM；否则不同窗口大小的 Agent 会产生错误压缩。

        profile 解析优先级：
        1. inbound metadata 的 agent_ref（team 工具显式携带，必须指向本 session）
        2. 成员 session 的 metadata['team_member'] 结构性绑定（任意入口都生效）
        3. 全局 agent（metadata.agent_id 或 default）
        """
        if turn.config is not None:
            return turn.config, turn.agent_profile

        config = copy.deepcopy(self._load_current_config())
        profile = None
        if self._loop.agent_manager is not None:
            from ftre.agent import sub_agent_profile

            inbound_metadata = turn.inbound.metadata

            # 路径 1：team 工具携带的 agent_ref。一致性校验：sub_agent 必须是
            # 本 session——metadata 外部可构造，不允许借它加载他人的 profile。
            agent_ref = inbound_metadata.agent_ref
            if (
                agent_ref is not None
                and agent_ref.sub_agent == turn.session_id
            ):
                profile = sub_agent_profile.load_member_profile(
                    self._loop.session_manager,
                    agent_ref.leader_session,
                    turn.session_id,
                )

            # 路径 2：session 级结构性绑定。WS/HTTP/send_message 等旁路入口
            # 不带 agent_ref，靠成员 session 自己的 team_member 绑定兜底。
            if profile is None:
                session_metadata = await self._loop.session_manager.get_session_metadata(
                    turn.session_id
                )
                binding = sub_agent_profile.binding_of(session_metadata)
                if binding is not None:
                    profile = sub_agent_profile.load_member_profile(
                        self._loop.session_manager,
                        binding["leader_session"],
                        turn.session_id,
                    )

            # 路径 3：全局 agent
            if profile is None:
                agent_id = inbound_metadata.agent_id or "default"
                profile = self._loop.agent_manager.load(agent_id)

        if profile is not None:
            # Agent 私有 llm 是实际模型配置；workspace 仍按现有规则由 session 决定。
            config.llm = copy.deepcopy(profile.llm)

        turn.agent_profile = profile
        turn.config = config
        return config, profile

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
        reply_id: str,
    ) -> tuple[list[dict], AgentConfig]:
        """构建发给 LLM 的消息列表，触发 before_messages_build hook。

        关键点：用户消息已在 _command 状态提前存到存储层，这里读
        get_context_messages()（summary + tail，已含本轮用户消息）。
        所以【不能再 append】当前用户输入，否则 LLM 会收到两份重复消息。
        完整 transcript 只服务 Desktop 历史展示，不进入 LLM 上下文。

        prompt_override（RewritePrompt 指令改写）通过覆盖最后一条 user 消息
        的 content 实现——存储层存原始，发给 LLM 用改写后的。
        """
        loop = self._loop
        # 读模型上下文（summary + tail，已含本轮用户消息，因为 _command 先存了）
        messages = await loop.session_manager.get_context_messages(session_id)

        # 触发 before_messages_build hook（插件可裁剪 Msg、生成标题、注入提示词）
        hook_config = copy.deepcopy(config)
        if loop.hook_manager is not None:
            from ftre.plugin import BEFORE_MESSAGES_BUILD, MessagesBuildContext

            ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=channel_id,
                inbound_data=inbound_data or {},
                workspace=workspace,
                reply_id=reply_id,
                agent_dir=agent_dir,
                config=hook_config,
                messages=messages,
            )
            ctx = await loop.hook_manager.trigger(BEFORE_MESSAGES_BUILD, ctx)
            hook_config = ctx.config
            messages = ctx.messages

        # 当前用户输入转成 OpenAI content 格式（文字 + 图片）
        user_content = build_user_content(
            content,
            attachments,
            include_images=hook_config.llm.vision,
        )

        if messages:
            from ftre.session.message.converter import to_openai

            # 持久化 Msg 转 OpenAI messages（已含本轮 user Msg）
            history = to_openai(
                messages,
                config={"llm": {"vision": hook_config.llm.vision}},
            )
            # 用户消息已在 _command 中提前持久化到 DB，to_openai 已包含它。
            # 不再 append（会导致 LLM 收到两份重复消息）。
            # 只有 RewritePrompt 确实提供 override 时才替换。普通消息已经由
            # Projection 原样落盘；无条件替换会在异常边界下误伤 compact 摘要。
            prompt_override = (
                ((inbound_data or {}).get("metadata") or {}).get("prompt_override")
            )
            if prompt_override is not None:
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
