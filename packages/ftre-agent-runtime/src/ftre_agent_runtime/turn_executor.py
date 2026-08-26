"""
TurnExecutor — 单个 Turn 的完整执行。

Turn 是一等公民：一个有状态的生命周期对象，从收到用户消息到响应完成。

状态机驱动：BUILDING → RUNNING → FINALIZING → COMPLETED。
execute() 只负责单个已经由 Inbox 交付的 Agent 输入。
Command 在接入层完成并不会进入本执行器。

处理路径：
  普通消息：  持久化用户消息 → BUILDING → RUNNING → FINALIZING → COMPLETED
  turn_cancel：由控制面取消当前 task，不进入本执行器
  确认恢复：已有 Session Event → BUILDING → RUNNING → FINALIZING → COMPLETED

本模块属于 ftre-agent-runtime：只消费 ``ftre_agent`` 公开契约和注入的 Host
Service 实例，不 import ``ftre.services.*`` 实现模块（PRD-F33 §5.4）。
"""

import asyncio
import copy
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from ftre_agent import (
    AGENT_RUN_ERROR_SPEC,
    AgentConfig,
    AgentRunResult,
    InboundMessage,
    RequestErrorPayload,
    RetryRequest,
)
from ftre_agent_core.agent import RunStatus
from ftre_agent_core.event import (
    CustomEvent,
    ReplyFinishedReason,
    UserConfirmResultEvent,
)
from ftre_agent_core.message import Msg

from .factory import compose_system_prompt, create_core_agent, default_agent_state
from .state import PUBLIC_RUN_STATUS, Turn, TurnStatus

if TYPE_CHECKING:
    from .engine import AgentLoop

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TurnExecutor
# ═══════════════════════════════════════════════════════════════════


class TurnExecutor:
    """单个 Turn 的完整执行：状态机驱动。

    AgentLoop 负责消费循环和并发控制，
    TurnExecutor 负责消息进来后的全部处理逻辑。
    """

    def __init__(
        self,
        loop: "AgentLoop",
        *,
        sessions=None,
        agents=None,
        attachments=None,
        system_prompt=None,
        hooks=None,
        agent_registry=None,
        tools=None,
        profiles=None,
        workspaces=None,
        config_service=None,
        llm_service=None,
    ) -> None:
        self._loop = loop
        self._sessions = sessions
        # These are explicit Provider dependencies.  Keeping them here avoids
        # using AgentLoop as a Service Locator from the turn data plane.
        self._agents = agents
        self._attachments = attachments
        self._system_prompt = system_prompt
        self._hooks = hooks
        self._agent_registry = agent_registry
        self._tools = tools
        self._profiles = profiles
        self._workspaces = workspaces
        self._config_service = config_service
        self._llm_service = llm_service
        # 测试可替换这一处纯构造函数；生产路径始终使用 Runtime 唯一工厂。
        self._core_factory = create_core_agent

    # ─── 驱动入口 ────────────────────────────────────────────

    async def execute(
        self,
        inbound: InboundMessage,
        *,
        turn_id: str | None = None,
        config: AgentConfig | None = None,
        agent_profile: Any | None = None,
        cancellation: asyncio.Event | None = None,
        confirm_event: UserConfirmResultEvent | None = None,
        user_message_id: str = "",
    ) -> AgentRunResult:
        """执行一条已经完成历史交接、由 Inbox 交付的 Agent 输入。"""
        session_id = self._session_id_of(inbound)

        turn = Turn(
            turn_id=turn_id or f"turn_{uuid.uuid4().hex[:12]}",
            inbound=inbound,
            session_id=session_id,
            config=config,
            agent_profile=agent_profile,
            cancellation=(cancellation if cancellation is not None else asyncio.Event()),
            confirm_event=confirm_event,
            user_message_id=user_message_id,
        )
        await self._emit_step(turn, "PIPELINE_START")
        return await self._drive(turn)

    async def _drive(self, turn: Turn) -> AgentRunResult:
        """推进一个已准备好的 Turn，并统一负责 stopping/finalize 边界。"""
        try:
            while turn.status not in (
                TurnStatus.COMPLETED,
                TurnStatus.CANCELLED,
                TurnStatus.ERROR,
            ):
                turn.status = await self._advance(turn)
        except asyncio.CancelledError:
            turn.status = TurnStatus.CANCELLED
            if turn.agent is not None:
                await self._persist_open_replies(turn, ReplyFinishedReason.INTERRUPTED)
        except Exception:
            logger.exception(
                f"[turn-executor] 状态机异常 session={turn.session_id} "
                f"status={turn.status}"
            )
            turn.status = TurnStatus.ERROR
        finally:
            # 无论正常完成、异常还是取消，都必须在离开 execute() 前关闭开放中的
            # reply/tool 生命周期并发送 PIPELINE_END。Inbox 在这里返回之后
            # 清除自己的内存运行态并唤醒同进程等待者；聊天事实已经写入 messages。
            if turn.agent is not None:
                await self._finalize(turn)
            await self._emit_step(
                turn,
                "PIPELINE_END",
                success=turn.status == TurnStatus.COMPLETED,
                reason=(
                    "error"
                    if turn.status == TurnStatus.ERROR
                    else "cancelled"
                    if turn.status == TurnStatus.CANCELLED
                    else ""
                ),
            )
        return AgentRunResult(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            status=PUBLIC_RUN_STATUS[turn.status],
            user_message_id=turn.user_message_id,
            final_content=turn.final_content,
            error=(
                {"code": "turn_error", "message": "Turn 执行失败", "retryable": True}
                if turn.status == TurnStatus.ERROR
                else None
            ),
        )

    async def _advance(self, turn: Turn) -> TurnStatus:
        """状态转移：根据当前状态调对应处理函数，返回下一个状态。"""
        match turn.status:
            case TurnStatus.BUILDING:
                return await self._build(turn)  # → RUNNING（或 ERROR 校验失败）
            case TurnStatus.RUNNING:
                return await self._run(turn)  # → FINALIZING/CANCELLED/ERROR
            case TurnStatus.FINALIZING:
                return TurnStatus.COMPLETED
            case _:
                return turn.status

    # ─── 状态处理函数 ────────────────────────────────────────

    async def _build(self, turn: Turn) -> TurnStatus:
        """[状态 2/4] 鉴权 + 构建消息 + 创建 Agent + 组装 runtime_context。

        这一步做完 Agent 就准备好了，下一步 _run 直接驱动它。
        校验失败会返回 ERROR（turn.agent 保持 None，不会进 _finalize 清理），
        由 AgentService 回传失败结果，避免入口误以为消息已经成功执行。
        """
        inbound = turn.inbound
        session_id = turn.session_id
        content = inbound.content
        attachments = list(inbound.attachments)

        # AgentLoop 已在历史交接前完成 Session/channel 校验。
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise RuntimeError(f"Session disappeared before Turn execution: {session_id}")

        # ── 取得本 Turn 已解析的有效配置（不同 agent 可用不同 LLM）──
        config, agent_profile = await self._resolve_turn_config(turn)

        # ── 权限确认恢复分支：注入历史 Msg 到新 agent，跳过普通消息构建 ──
        if turn.confirm_event is not None:
            return await self._build_resume(turn, session, config, agent_profile)

        # ── 构建发给 LLM 的消息 ──
        workspace = session.get("workspace", "") or config.workspace or os.getcwd()
        # 发送消息时确保当前工作区有 .ftre 扩展目录骨架（工作区级 skill / mcp.json 的落点）
        if self._workspaces is not None:
            await self._workspaces.ensure_extension_layout(session_id)
        messages, hook_config = await self._build_messages(
            session_id,
            content,
            attachments,
            config,
        )
        hook_config = await self._assemble_prompt(
            turn,
            hook_config,
            messages,
            workspace=workspace,
        )
        # 轮后压缩屏障必须继续使用真正创建 Agent 时的配置，而不是重新读取
        # 此刻可能已经切换过的 default Agent 配置。
        turn.config = copy.deepcopy(hook_config)
        if agent_profile is not None:
            # create_agent() 也会以 profile.llm 为最终值；这里保持快照与真实
            # Agent 完全一致，避免 hook/test double 返回了另一套 llm。
            turn.config.llm = copy.deepcopy(agent_profile.llm)
        # ── 创建本轮私有 Agent；取消由 AgentService 持有的 Turn task 传播 ──
        await self._create_agent(
            turn,
            hook_config,
            agent_profile,
            workspace=workspace,
        )

        # Prompt Hook 只能替换结构化 assembly；消息历史保持由 Session/Prompt
        # 正式构建，避免 Feature 就地修改首个 system message。
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
        session_id = turn.session_id
        workspace = session.get("workspace", "") or config.workspace or os.getcwd()

        # 只读 LLM 有效上下文（最后一条 compact 摘要 + tail），避免把已经被摘要
        # 覆盖的完整 transcript 重新注入。保留 typed Msg 以维持 ToolCallState。
        records = await self._sessions.get_context_messages(session_id)
        hook_config = copy.deepcopy(config)
        hook_config = await self._assemble_prompt(
            turn,
            hook_config,
            tuple(records),
            workspace=workspace,
        )
        context_msgs = [self._sessions.record_to_msg(r) for r in records]
        # 复用默认权限规则，注入历史 context
        state = default_agent_state()
        state.context = context_msgs
        turn.config = copy.deepcopy(hook_config)
        if agent_profile is not None:
            # 与普通 Turn 保持一致：运行时配置快照必须和 AgentManager
            # 最终应用的 profile.llm 相同，供后续压缩/诊断读取。
            turn.config.llm = copy.deepcopy(agent_profile.llm)
        await self._create_agent(
            turn,
            hook_config,
            agent_profile,
            workspace=workspace,
            state=state,
            reply_id=turn.confirm_event.reply_id,
        )
        return TurnStatus.RUNNING

    async def _create_agent(
        self,
        turn: Turn,
        config: AgentConfig,
        agent_profile,
        *,
        workspace: str,
        state=None,
        reply_id: str | None = None,
    ) -> None:
        """Create the Core Agent and its shared tool context for both Turn paths."""
        loop = self._loop
        inbound = turn.inbound
        metadata = dict(inbound.metadata or {})
        agent_id = agent_profile.agent_id if agent_profile is not None else str(
            metadata.get("agent_id") or "default"
        )
        core_hooks, core_hook_context = self._core_hook_binding(turn)
        effective_llm_config = (
            agent_profile.llm if agent_profile is not None else config.llm
        )
        tool_view = await self._tools.prepare_view(
            agent_id,
            turn.session_id,
            agent_profile,
            llm_config=effective_llm_config,
        )
        service_adapter = None
        provider = getattr(effective_llm_config, "provider", "")
        if self._llm_service is not None and provider:
            from ftre_llm import LlmCallConfig, LlmCredentials, LlmServiceAdapter

            service_adapter = LlmServiceAdapter(
                self._llm_service,
                LlmCallConfig(
                    provider=provider,
                    model=effective_llm_config.model,
                    api_type=effective_llm_config.api_type,
                    max_tokens=effective_llm_config.max_output,
                    reasoning_effort=effective_llm_config.reasoning_effort,
                ),
                LlmCredentials(
                    api_key=effective_llm_config.api_key,
                    api_base=effective_llm_config.api_base,
                ),
                agent_id=str(metadata.get("agent_id") or "default"),
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                cancellation=turn.cancellation,
            )
        system_prompt = compose_system_prompt(
            config,
            agent_profile,
            channel_id=inbound.channel_id,
            session_id=turn.session_id,
        )
        runtime_agent_id = str(metadata.get("agent_id") or "default")
        effective_config = turn.config if turn.config is not None else config
        turn.agent = self._core_factory(
            config=config,
            profile_snapshot=agent_profile,
            tool_view=tool_view,
            system_prompt=system_prompt,
            tracer=loop.tracer,
            hooks=core_hooks,
            hook_context=core_hook_context,
            state=state or default_agent_state(),
            llm=service_adapter,
        )
        turn.runtime_context = {
            "session_id": turn.session_id,
            "request_id": inbound.request_id,
            "agent_id": runtime_agent_id,
            "agent_subject": loop.agent_subject(runtime_agent_id),
            "channel_id": inbound.channel_id,
            "event_loop": loop._event_loop,
            "sessions": self._sessions,
            "bus": loop.message_bus,
            "agent": self._agents,
            "attachments": self._attachments,
            "llm_config": effective_config.llm,
            "agent_profile": agent_profile,
            "workspace": self._workspaces.create_accessor(
                turn.session_id,
                loop._event_loop,
                fallback_cwd=workspace,
            ),
            "trace_name": f"session:{turn.session_id}",
            "trace_tags": [inbound.channel_id or "unknown"],
            "trace_metadata": {
                "session_id": turn.session_id,
                "channel_id": inbound.channel_id,
                "workspace": workspace,
            },
            "reply_id": reply_id or turn.turn_id,
            "cancellation": turn.cancellation,
            "continuation_count": turn.continuation_count,
            "max_continuations": turn.max_continuations,
        }

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
                # Event 逐条交给 SessionProjection；Projection 按 message_id 聚合并在
                # 语义屏障 checkpoint，REPLY_END 只负责当前 Assistant 的最终收尾。
                completed_message = await self.publish_agent_event(turn, event)
                if completed_message is not None:
                    turn.final_content = completed_message.get_text_content() or ""

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
            if _is_error and await self._request_error_recovery(
                turn,
                error_code=run_state.error_code or "request_error",
                message=run_state.error or "Agent request failed",
            ):
                return TurnStatus.RUNNING
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
            logger.info(
                f"[turn-executor] Agent 被 cancel 中断 session={turn.session_id}"
            )
            await self._persist_open_replies(turn, ReplyFinishedReason.INTERRUPTED)
            # 仍发 TURN_END，让实时客户端立即结束本轮 busy 状态
            await self._emit_step(
                turn,
                "TURN_END",
                success=False,
                reason=str(ReplyFinishedReason.INTERRUPTED),
            )
            return TurnStatus.CANCELLED
        except Exception:
            # 未预期异常
            logger.exception(f"[turn-executor] _run 异常 (session={turn.session_id})")
            if await self._request_error_recovery(
                turn, error_code="request_exception", message="Agent request raised"
            ):
                return TurnStatus.RUNNING
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
        """[状态 4/4] 收尾：清理 Agent 注册并通知 subagent。

        在 execute() 的 finally 里统一调用（无论正常/取消/异常都会走），
        所以它是 Turn 的唯一收尾出口，必须幂等且不抛异常。
        """
        # ── 摘除 _active_agents（仅当还是自己创建的那个）──
        # 若已被后来的 Agent 顶替，不清理（避免误删别人的）
        # 无条件 emit（不依赖通道推断）：task/team 只对 subagent session wait，
        # 非 subagent 完成时无人订阅，零开销；未来其它功能可订阅同一事件。
        # request 完成的等待与通知由 Inbox 的进程内 receipt 处理。

        return TurnStatus.COMPLETED

    # ─── 事件发布 ──────────────────────────────────────────

    async def _emit_step(self, turn: Turn, phase: str, **kwargs) -> None:
        """构造 CustomEvent 并实时推送。

        所有 Turn 边界事件（PIPELINE_START/END、
        TURN_START/END）都走这里，用 CustomEvent 携带 phase 信息。
        reply_id 关联到 turn.turn_id（通过 metadata 传递，CustomEvent 无 reply_id 字段）。
        """
        event = CustomEvent(
            name=phase,
            value=kwargs,
            metadata={"reply_id": turn.turn_id},
        )
        await self.publish_agent_event(turn, event)

    async def publish_agent_event(self, turn: Turn, event) -> Msg | None:
        """实时派发 Event，并在 Reply 生命周期内管理 Msg 持久化。

        - REPLY_START: 创建 AssistantMsg + save_message + 注册 ActiveReplyRegistry
        - 其他 Event: append_event + checkpoint（节流/立即）
        - REPLY_END: append_event + update_message + 注销 registry
        - CustomEvent(context_compact_done): 投影为 user/compact Msg 并落盘
        """
        result = await self._loop.emit_session_event(
            turn.session_id,
            turn.inbound.channel_id,
            event,
            metadata=dict(turn.inbound.metadata or {}),
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
        for message in await self._sessions.finish_open_replies(
            turn.session_id, reason, error=error
        ):
            turn.final_content = message.get_text_content() or turn.final_content

    async def _assemble_prompt(
        self,
        turn: Turn,
        config: AgentConfig,
        messages,
        *,
        workspace: str,
    ) -> AgentConfig:
        """Render structured sections, then run the typed assembly waterfall.

        组装与 ``system-prompt/assemble`` Hook 都由注入的 SystemPromptService
        完成；Runtime 只传入本轮上下文。该 Hook 的 Owner 是 system_prompt
        域，Runtime 不 import 它的 Spec/Payload 类型（PRD-F33 §5.4）。
        """
        loop = self._loop
        metadata = dict(turn.inbound.metadata or {})
        agent_id = str(metadata.get("agent_id") or "default")
        updated = copy.deepcopy(config)
        if self._system_prompt is None:
            # 无 Prompt Service 的独立测试环境：与 default waterfall 等价，
            # 保持 config.system_prompt 原样返回。
            return updated
        hooks = loop.hooks
        scope_context = None
        if hooks is not None:
            registry = loop.agent_registry
            registry.ensure(agent_id)
            scope_context = hooks.context_for_scope(registry.scope_carrier(agent_id))
        assembly = await self._system_prompt.assemble_agent_prompt(
            agent_subject=loop.agent_subject(agent_id),
            session_id=turn.session_id,
            workspace=workspace,
            messages=messages,
            base_prompt=config.system_prompt,
            inbound_data={
                "session_id": turn.inbound.session_id,
                "request_id": turn.inbound.request_id,
                "content": turn.inbound.content,
                "attachments": [dict(item) for item in turn.inbound.attachments],
                "source": turn.inbound.source,
                "metadata": metadata,
            },
            config=config,
            hook_runtime=hooks,
            scope_context=scope_context,
            event_loop=loop._event_loop,
            cancellation=turn.cancellation,
        )
        updated.system_prompt = assembly.text
        return updated

    def _core_hook_binding(self, turn: Turn):
        """Return the host Dispatcher and Cordis scope for this Agent/Turn."""
        hooks = self._hooks
        registry = self._agent_registry
        agent_id = str(dict(turn.inbound.metadata or {}).get("agent_id") or "default")
        if hooks is None or registry is None:
            return None, None
        # scope_carrier 要求 identity 已登记；ensure 幂等，重复调用安全。
        registry.ensure(agent_id)
        return hooks, hooks.context_for_scope(registry.scope_carrier(agent_id))

    async def _request_error_recovery(
        self, turn: Turn, *, error_code: str, message: str
    ) -> bool:
        """Run request-error waterfall and accept only bounded progress tokens."""
        loop = self._loop
        metadata = dict(turn.inbound.metadata or {})
        agent_id = str(metadata.get("agent_id") or "default")
        payload = RequestErrorPayload(
            agent=loop.agent_subject(agent_id),
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            error_code=error_code,
            message=message,
            attempt=turn.retry_count,
            cancellation=turn.cancellation,
            channel_id=turn.inbound.channel_id,
            config=copy.deepcopy(turn.config),
        )
        try:
            result = await loop.dispatch_agent_hook(
                AGENT_RUN_ERROR_SPEC, payload, agent_id=agent_id
            )
        except Exception:
            logger.exception(
                "[turn-executor] agent/run-error failed session=%s",
                turn.session_id,
            )
            return False
        if turn.cancellation.is_set() or not isinstance(result, RetryRequest):
            return False
        if result.progress_token in turn.retry_tokens:
            return False
        if turn.retry_count >= result.max_attempts:
            return False
        turn.retry_tokens.add(result.progress_token)
        turn.retry_count += 1
        await self._emit_step(
            turn,
            "REQUEST_RETRY",
            reason=result.reason,
            attempt=turn.retry_count,
        )
        return True

    # ─── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _session_id_of(inbound: InboundMessage) -> str:
        """读取已归一化 InboundMessage 的 Session 身份。"""
        return inbound.session_id

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置（测试注入优先，否则从磁盘加载）。"""
        loop = self._loop
        if loop._injected_config is not None:
            return loop._injected_config
        if self._config_service is None:
            return AgentConfig()
        return self._config_service.resolve_agent_config()

    async def resolve_inbound_config(
        self, inbound: InboundMessage, *, turn_id: str
    ) -> tuple[AgentConfig, Any | None]:
        """解析 Hook 门控和实际执行共同使用的精确配置快照。"""
        turn = Turn(
            turn_id=turn_id,
            inbound=inbound,
            session_id=self._session_id_of(inbound),
        )
        return await self._resolve_turn_config(turn)

    async def _resolve_turn_config(
        self, turn: Turn
    ) -> tuple[AgentConfig, Any | None]:
        """取得并缓存本 Turn 真正使用的 Agent 配置。

        context_window、模型调用和回复结束后的轮后 Hook 屏障必须来自同一份快照。
        不能在 Hook 判断时只读全局/default 配置，再在 _build 阶段才覆盖
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
        session_model = await self._sessions.get_session(turn.session_id)
        session_agent_id = (
            str(session_model.get("agent_id") or "default")
            if session_model is not None
            else "default"
        )
        metadata = dict(turn.inbound.metadata or {})
        agent_id = str(metadata.get("agent_id") or session_agent_id)
        if self._profiles is not None:
            snapshot = await self._profiles.resolve_for_inbound(
                agent_id,
                turn.session_id,
                metadata=metadata,
            )
            profile = getattr(snapshot, "value", snapshot)

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
    ) -> tuple[list[dict], AgentConfig]:
        """构建发给 LLM 的消息列表。

        关键点：用户消息已在 Agent Work Item 接纳后持久化，这里读
        get_context_messages()（summary + tail，已含本轮用户消息）。
        所以【不能再 append】当前用户输入，否则 LLM 会收到两份重复消息。
        完整 transcript 只服务 Desktop 历史展示，不进入 LLM 上下文。

        消息格式转换（content parts、OpenAI dict、typed Msg）由注入的
        SessionService 窄方法完成；Runtime 不 import Host 的转换模块。
        """
        # 读模型上下文（summary + tail，已含本轮用户消息）。
        messages = await self._sessions.get_context_messages(session_id)

        hook_config = copy.deepcopy(config)

        # 当前用户输入转成 OpenAI content 格式（文字 + 图片）
        user_content = self._sessions.build_user_content(
            content,
            attachments,
            include_images=hook_config.llm.vision,
        )

        if messages:
            # 持久化 Msg 转 OpenAI messages（已含本轮 user Msg）
            history = self._sessions.to_openai_messages(
                messages,
                vision=hook_config.llm.vision,
            )
            return history, hook_config

        # 无历史（首条消息）：直接用当前输入
        return [{"role": "user", "content": user_content}], hook_config
