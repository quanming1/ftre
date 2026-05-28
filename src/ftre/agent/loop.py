"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 收到 user_input 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
- 收到 cancel 时，通知 Agent 中断执行
"""
import asyncio
import logging
import os
import threading

from ftre_agent_core.agent import ReActAgent
from ftre.bus import BusMessage, EventBus
from ftre.config import AgentConfig, load_config
from ftre.session import SessionManager
from ftre.session.multimodal import build_user_content
from ftre.tools import build_default_tools
from ftre.tools._workspace import WorkspaceAccessor

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全局单例，消费所有 session 的消息。

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent
    """

    def __init__(self, bus: EventBus, session_manager: SessionManager, channel_manager=None, config: AgentConfig = None):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        # 注入的 config 优先（测试场景）；否则每次 _create_agent 都重新读盘，
        # 让 UI 改完 ~/.ftre/config.json 立即生效。
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # session_id → 当前正在执行的 agent（用于取消）
        self._active_agents: dict[str, ReActAgent] = {}

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。AgentLoop 在 _run finally 里
        必定 pop，所以这是"跑完没"的权威信号——不依赖 done 事件是否被发出。"""
        return session_id in self._active_agents

    async def stop(self) -> None:
        """停止消费循环并中断所有正在运行的 Agent"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for agent in list(self._active_agents.values()):
            agent.cancel_nowait()
        self._active_agents.clear()

    async def _consume(self) -> None:
        """
        消费循环：从 Bus 全局 inbound 队列读取消息。

        - user_input → 在线程池中执行 _run()
        - cancel     → 发送取消信号给对应 session 的 Agent
        """
        try:
            async for msg in self.bus.subscribe_inbound():
                if msg.type == "user_input":
                    asyncio.ensure_future(
                        asyncio.get_event_loop().run_in_executor(None, self._run, msg)
                    )
                elif msg.type == "cancel":
                    sid = msg.from_session or msg.data.get("session_id", "")
                    agent = self._active_agents.get(sid)
                    if agent is not None:
                        agent.cancel_nowait()
                    else:
                        logger.warning(f"[agent-loop] cancel: 未找到活跃 agent (session={sid})")
        except asyncio.CancelledError:
            pass

    # 需要持久化的事件类型（小写，和 EventType 枚举值一致）
    PERSISTENT_EVENTS = {
        "message_complete",
        "reasoning_complete",
        "tool_call",
        "tool_result",
        "tool_cancel_requested",
        "tool_cancelled",
        "tool_timed_out",
        "usage_update",
        "error",
        "done",
    }

    # ─── 上下文治理参数 ─────────────────────────────────────
    # 水位（total_tokens / context_window）超过此阈值就触发裁剪
    CONTEXT_PRESSURE_THRESHOLD = 0.6
    # 保护最近 N 轮（一轮 = 一个 USER_INPUT 到下一个 USER_INPUT 之前）
    PROTECTED_ROUNDS = 2
    # tool_result 被裁剪后保留的 result 前缀长度（字符）
    TOOL_RESULT_TRUNCATE_KEEP = 500
    # 裁剪占位提示
    TOOL_RESULT_TRUNCATE_HINT = "\n\n…[已裁剪以节省上下文]"

    def _run(self, inbound: BusMessage) -> None:
        """
        在线程中执行 Agent，事件逐条投递回 Bus。

        流程：
        - Step 1: 入参校验（content / session_id 不为空）
        - Step 2: 鉴权（session 必须存在，且 channel 与消息来源一致）
        - Step 3: 构造 user content（折叠多模态）+ 加载配置
        - Step 4: 加载历史 + 拼接当前用户消息（含上下文治理）
        - Step 5: 创建独立 Agent + 注入上下文
        - Step 6: 持久化用户输入
        - Step 7: 驱动 Agent 执行，逐事件持久化 + 推送
        - Step 8: 异常兜底：保证一定有 done 事件投递
        - Step 9: 清理 _active_agents 引用
        """
        # Step 1: 入参校验
        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []
        if not content and not attachments:
            return

        session_id = inbound.data.get("session_id", "")
        if not session_id:
            logger.warning("[agent-loop] 收到无 session_id 的消息，忽略")
            return

        # Step 2: 鉴权 — session 必须真实存在，且 channel 与消息来源一致，
        # 避免别的 channel 伪造 session_id 投递任务、或对已删除会话的残留消息执行
        session = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_session(session_id),
            self._event_loop,
        ).result()
        if session is None:
            logger.warning(
                f"[agent-loop] session 不存在，拒绝执行: "
                f"channel={inbound.from_channel}, session={session_id}"
            )
            return
        if session["channel_id"] != inbound.from_channel:
            logger.warning(
                f"[agent-loop] session 与 channel 不匹配，拒绝执行: "
                f"session={session_id} (channel={session['channel_id']}), "
                f"消息来自 channel={inbound.from_channel}"
            )
            return

        # Step 3: 构造 user content + 加载配置
        # 无附件返回 str；有附件返回 list[part]
        user_content = build_user_content(content, attachments)
        # 每次都建一个独立 agent，彻底避免跨 session 串扰
        # 同时每次重新读取配置文件，UI 改 model/provider/system_prompt 立即生效
        config = self._load_current_config()

        # Step 4: 加载历史 + 拼接当前用户消息（含上下文治理）
        messages = self._build_messages(session_id, user_content, config)

        # Step 4.5: 首条用户消息 → 异步生成标题（不阻塞主流程）
        # 判定"首条"：session 在 DB 中没有 title 且尚未持久化任何 USER_INPUT。
        # 现在还没存当前帧的 USER_INPUT（Step 6），所以这里检查最干净。
        # 失败/超时不影响 agent 执行，标题只是 UI 优化项。
        if self._is_first_user_message(session, session_id):
            self._spawn_title_generation(
                session_id=session_id,
                user_content=user_content,
                config=config,
            )

        # Step 5: 创建独立 Agent + 注入运行时上下文
        agent = self._create_agent(config)
        agent.system_prompt = (
            config.system_prompt
            + f"\n\n[当前上下文] channel_id={inbound.from_channel}, session_id={session_id}"
        )
        self._active_agents[session_id] = agent

        # Step 6: 持久化用户输入
        asyncio.run_coroutine_threadsafe(
            self.session_manager.save_message(session_id, "USER_INPUT", inbound.data),
            self._event_loop,
        ).result()

        # Step 6.5: 把 user_input 也作为 agent_event 下行
        # 这样目标 session 的前端无论是不是自己发的（如被 send_message 跨 session 唤起），
        # 都能拿到这条用户消息渲染。inbound.metadata.frame_id 是发送端协议帧 id，
        # 透传给前端作为占位去重 key（本地已有同 id 消息时丢弃，否则 push）。
        echo = BusMessage(
            type="agent_event",
            from_channel=inbound.from_channel,
            to_channel=inbound.to_channel,
            from_session=inbound.from_session,
            to_session=inbound.to_session,
            data={"type": "user_input", "data": inbound.data},
            metadata=inbound.metadata,
        )
        asyncio.run_coroutine_threadsafe(
            self.bus.publish_outbound(echo), self._event_loop
        ).result()

        # Step 7: 驱动 Agent 执行
        # workspace 是一个 WorkspaceAccessor —— 对 sessions.workspace 字段的同步外观。
        # 工具拿到它后用 ws.get() / ws.set(...) 读写持久化的 cwd，不再有内存中转 dict。
        # 默认值（DB 中 workspace 为空时回退）：config.workspace > 进程 cwd。
        cfg_ws = (config.workspace or "").strip()
        fallback = (
            os.path.abspath(cfg_ws) if cfg_ws and os.path.isdir(cfg_ws) else os.getcwd()
        )
        runtime_context = {
            "session_id": session_id,
            "channel_id": inbound.from_channel,
            "event_loop": self._event_loop,
            "bus": self.bus,
            "session_manager": self.session_manager,
            "agent_loop": self,
            "workspace": WorkspaceAccessor(
                session_id=session_id,
                session_manager=self.session_manager,
                event_loop=self._event_loop,
                fallback_cwd=fallback,
            ),
        }

        try:
            for event in agent.run(messages, runtime_context=runtime_context):
                # Step 7a: 持久事件存储
                if event.get("type") in self.PERSISTENT_EVENTS:
                    asyncio.run_coroutine_threadsafe(
                        self.session_manager.save_message(session_id, event["type"], event.get("data", {})),
                        self._event_loop,
                    ).result()

                # Step 7b: 推送给前端
                out = BusMessage(
                    type="agent_event",
                    from_channel=inbound.from_channel,
                    to_channel=inbound.to_channel,
                    from_session=inbound.from_session,
                    to_session=inbound.to_session,
                    data=event,
                )
                asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(out), self._event_loop).result()
        except Exception:
            # Step 8: 异常兜底 — 保证一定有 done 事件投递，避免前端永远卡在"思考中"
            logger.exception(f"[agent-loop] _run 异常 (session={session_id})")
            err_evt = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data={"type": "done", "data": {"success": False, "reason": "error"}},
            )
            asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(err_evt), self._event_loop).result()
        finally:
            # Step 9: 清理活跃引用，让 GC 回收
            if self._active_agents.get(session_id) is agent:
                self._active_agents.pop(session_id, None)

    def _load_current_config(self) -> AgentConfig:
        """
        读取当前生效的配置：
        - 若构造时显式注入了 config（如测试），始终用注入值
        - 否则每次都重新读取 ~/.ftre/config.json，让 UI 改动立即生效
        """
        if self._injected_config is not None:
            return self._injected_config
        return load_config()

    def _build_messages(
        self,
        session_id: str,
        user_content: str | list[dict],
        config: AgentConfig,
    ) -> str | list[dict]:
        """
        构建一次 LLM 调用的输入消息（含上下文治理）。

        - 有历史：回放历史事件为 OpenAI messages，再追加当前用户消息
        - 无历史 + 纯文本 user_content：返回 str，走 ReActAgent 的快速路径
        - 无历史 + 多模态 user_content：必须包一层 list[{role, content}]

        上下文治理：水位 (total_tokens / context_window) > CONTEXT_PRESSURE_THRESHOLD
        时，对最后 PROTECTED_ROUNDS 轮以外的 tool_result 做长度裁剪。
        """
        events = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_messages_by_session(session_id),
            self._event_loop,
        ).result()

        # 先消除孤立的 tool_call / tool_result（OpenAI 协议要求两者必须配对）
        events = self._drop_orphan_tool_events(events)

        # 上下文治理：必要时裁剪老 tool_result 的输出
        events = self._govern_context(session_id, events, config)

        if events:
            history = SessionManager.to_openai_messages(events)
            history.append({"role": "user", "content": user_content})
            return history

        # 无历史快速路径：纯字符串直接返回；多模态必须包成 message
        if isinstance(user_content, str):
            return user_content
        return [{"role": "user", "content": user_content}]

    def _drop_orphan_tool_events(self, events: list) -> list:
        """
        丢弃孤立的 tool_call / tool_result 事件。

        OpenAI 协议要求：assistant.tool_calls 必须有对应 role=tool 的 tool_result
        相邻配对，反过来也是。事件流里如果出现：
        - tool_call(id=X) 但没有 tool_result(id=X)（如 cancelled / timed_out / 崩溃）
        - tool_result(id=X) 但没有 tool_call(id=X)（老数据 / 协议错配）

        都会导致后续 LLM 调用 400 报错。这里直接丢弃。

        匹配维度：data.id（OpenAI tool_call_id）
        """
        if not events:
            return events

        call_ids: set[str] = set()
        result_ids: set[str] = set()
        for ev in events:
            t = ev.get("type")
            if t == "tool_call":
                call_ids.add((ev.get("data") or {}).get("id", ""))
            elif t == "tool_result":
                result_ids.add((ev.get("data") or {}).get("id", ""))

        paired = call_ids & result_ids
        # 没有孤立项就直接返回（避免拷贝）
        if len(call_ids) == len(paired) and len(result_ids) == len(paired):
            return events

        dropped = 0
        out: list = []
        for ev in events:
            t = ev.get("type")
            if t in ("tool_call", "tool_result"):
                if (ev.get("data") or {}).get("id", "") not in paired:
                    dropped += 1
                    continue
            out.append(ev)

        if dropped:
            logger.info(f"[govern] 丢弃 {dropped} 个孤立 tool 事件")
        return out

    def _ensure_tool_call_result_adjacency(self, events: list) -> list:
        """
        保证每个 tool_call 紧跟着对应的 tool_result。

        触发场景：tool_call 之后插入了 external_message 等事件，再之后才到达
        tool_result（task 工具阻塞期间会有 cron 派发的 external_message 进来）。
        to_openai_messages 把这种结构折成 OpenAI messages 时，tool_call 被先
        flush 成 assistant message，后续 tool_result 跟它不再相邻 → LLM 投诉
        'tool_call_ids did not have response messages'。

        修复策略：扫一遍事件，对每个 tool_call 找到配对的 tool_result，把
        tool_result 移到紧贴 tool_call 之后；中间被打断的事件（如
        external_message）顺位后移。

        没有打断时返回原列表（零拷贝）。
        """
        if not events:
            return events

        # 收集 (tool_call_idx, result_idx)
        call_idx_by_id: dict[str, int] = {}
        pairs: list[tuple[int, int]] = []
        for i, ev in enumerate(events):
            t = ev.get("type")
            data = ev.get("data") or {}
            tid = data.get("id", "")
            if not tid:
                continue
            if t == "tool_call":
                call_idx_by_id[tid] = i
            elif t == "tool_result" and tid in call_idx_by_id:
                call_i = call_idx_by_id.pop(tid)
                if i != call_i + 1:
                    # 不相邻才需要修复
                    pairs.append((call_i, i))

        if not pairs:
            return events

        # 重建：按原序遍历，遇到需要修复的 tool_call 时立刻插入它的 result，
        # 跳过原位置的那条 result
        result_indices_to_skip = {result_i for _, result_i in pairs}
        moves: dict[int, int] = {call_i: result_i for call_i, result_i in pairs}

        out: list = []
        for i, ev in enumerate(events):
            if i in result_indices_to_skip:
                continue
            out.append(ev)
            if i in moves:
                out.append(events[moves[i]])

        logger.info(f"[govern] 修复 {len(pairs)} 对不相邻的 tool_call/tool_result")
        return out

    def _govern_context(
        self,
        session_id: str,
        events: list,
        config: AgentConfig,
    ) -> list:
        """
        上下文治理，分两步：

        1) tool_call / tool_result 必须相邻（OpenAI 协议要求）。
           中间被 external_message / 其他事件打断时，把 tool_result 提到
           对应 tool_call 之后，保证 to_openai_messages 输出合规。

        2) 水位 (total_tokens / context_window) > CONTEXT_PRESSURE_THRESHOLD
           时，对最后 PROTECTED_ROUNDS 轮以外的 tool_result 做长度裁剪。

        裁剪规则：
        - 找到最后 PROTECTED_ROUNDS 个 USER_INPUT 之中最早的那个，作为保护边界
        - 该边界之前所有 tool_result 事件的 data.result 截断到 TOOL_RESULT_TRUNCATE_KEEP
        - 保护边界内的事件原样保留
        - 数据库不变，仅修改内存中的 events 副本

        返回：可能被裁剪过的新 events 列表（不修改入参）
        """
        # Step 1: 修复 tool_call / tool_result 相邻
        events = self._ensure_tool_call_result_adjacency(events)

        cw = config.llm.context_window
        if not cw or cw <= 0 or not events:
            return events

        # 取真实水位（最近一次 LLM 实算 + pending 估算）
        total = self._get_total_tokens(session_id)
        ratio = total / cw
        if ratio <= self.CONTEXT_PRESSURE_THRESHOLD:
            return events

        # 找最近 PROTECTED_ROUNDS 个 USER_INPUT 中最早那个的 index
        user_input_indices = [
            i for i, ev in enumerate(events) if ev.get("type") == "USER_INPUT"
        ]
        if len(user_input_indices) <= self.PROTECTED_ROUNDS:
            # 历史不够 N 轮，没什么可裁的
            return events
        boundary = user_input_indices[-self.PROTECTED_ROUNDS]

        # 复制并裁剪 boundary 之前的 tool_result
        truncated_count = 0
        out: list = []
        for i, ev in enumerate(events):
            if i >= boundary or ev.get("type") != "tool_result":
                out.append(ev)
                continue
            data = ev.get("data") or {}
            result = data.get("result", "")
            if isinstance(result, str) and len(result) > self.TOOL_RESULT_TRUNCATE_KEEP:
                new_data = dict(data)
                new_data["result"] = (
                    result[: self.TOOL_RESULT_TRUNCATE_KEEP]
                    + self.TOOL_RESULT_TRUNCATE_HINT
                )
                # 浅拷贝事件，原引用不动
                new_ev = dict(ev)
                new_ev["data"] = new_data
                out.append(new_ev)
                truncated_count += 1
            else:
                out.append(ev)

        if truncated_count:
            logger.info(
                f"[govern] session={session_id} 水位 {ratio:.0%} "
                f"({total}/{cw})，裁剪 {truncated_count} 条 tool_result，"
                f"保护最近 {self.PROTECTED_ROUNDS} 轮"
            )
        return out

    def _get_total_tokens(self, session_id: str) -> int:
        """从 SessionManager 取该 session 的 token 总量（实算 + pending 估算）"""
        usage = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_token_usage(session_id),
            self._event_loop,
        ).result()
        return int(usage.get("total", 0) or 0)

    def _create_agent(self, config: AgentConfig) -> ReActAgent:
        """根据配置创建 ReActAgent 实例"""
        c = config
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=build_default_tools(channel_manager=self.channel_manager),
            max_iterations=c.max_iterations,
        )

    # ============================================================
    # 标题生成（首条用户消息）
    # ============================================================

    # 标题生成的 prompt（中文模型默认；不限制语言，按用户输入语言走）
    TITLE_GEN_SYSTEM_PROMPT = (
        "你是一个标题生成器。给定一段用户消息，给出一个20字以内的简短标题，"
        "概括用户的意图。只输出标题本身，不要引号、不要标点、不要前缀如『标题：』。"
    )
    # 截断长度：标题在 LLM 看到的输入也只取前 N 字符，避免大块代码 / 日志被全量塞进去
    TITLE_GEN_INPUT_TRUNCATE = 1000
    # 落库前对标题再做一道硬截断，防止模型不听话输出超长字符串
    TITLE_MAX_CHARS = 40

    def _is_first_user_message(self, session: dict | None, session_id: str) -> bool:
        """
        判断当前 inbound 是否是 session 的首条用户消息。

        条件：session 存在 + title 为空 + DB 里还没有任何 USER_INPUT 事件。
        title 已经有值时不再覆盖（避免重命名后的 session 被首条逻辑改回去）。
        """
        if not session:
            return False
        if (session.get("title") or "").strip():
            return False
        # 此时 Step 6 还没保存当前帧；如果一条 USER_INPUT 都没有 → 首条
        events = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_messages_by_session(session_id),
            self._event_loop,
        ).result()
        return not any(ev.get("type") == "USER_INPUT" for ev in events)

    def _spawn_title_generation(
        self,
        session_id: str,
        user_content: str | list[dict],
        config: AgentConfig,
    ) -> None:
        """
        起一个守护线程跑 LLM 生成标题，落库即可。
        前端有 sessions 列表轮询，新 title 自然会刷新出来，
        所以这里不主动推送事件。失败/取消静默吞掉。

        实现细节：_run 本身已经在 executor 线程里，子线程不能再 asyncio.get_event_loop()
        （Python 3.12 起会抛 RuntimeError）。直接 spawn 一个新线程，worker 内通过保留
        的 self._event_loop 引用 + run_coroutine_threadsafe 回主 loop 落库。
        """
        # 取出文本部分；多模态 user_content 里可能没有 text，这种情况下用占位
        text = self._extract_text_for_title(user_content)
        if not text:
            return
        text = text[: self.TITLE_GEN_INPUT_TRUNCATE]

        loop = self._event_loop

        def worker() -> None:
            try:
                title = self._generate_title_via_llm(text, config)
            except Exception:
                logger.exception(f"[agent-loop] 生成标题失败 (session={session_id})")
                return
            if not title:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self.session_manager.update_session(session_id, title=title),
                    loop,
                ).result()
            except Exception:
                logger.exception(f"[agent-loop] 写入标题失败 (session={session_id})")
                return
            logger.info(f"[agent-loop] 已生成标题 session={session_id} title={title!r}")

        threading.Thread(
            target=worker,
            name=f"title-gen-{session_id}",
            daemon=True,
        ).start()

    @staticmethod
    def _extract_text_for_title(user_content: str | list[dict]) -> str:
        """从 user_content 里提取纯文本部分；多模态时只看 text part。"""
        if isinstance(user_content, str):
            return user_content.strip()
        if isinstance(user_content, list):
            chunks: list[str] = []
            for part in user_content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text", "")
                    if isinstance(t, str) and t:
                        chunks.append(t)
            return "\n".join(chunks).strip()
        return ""

    def _generate_title_via_llm(self, user_text: str, config: AgentConfig) -> str:
        """
        一次性 LLM 调用生成标题。无工具、不带历史、流式收集后拼接。

        使用 config.title_llm（如果配置了），否则回退到主 llm。

        Returns:
            清洗后的标题字符串；若 LLM 没返回内容或被取消则返回空串。
        """
        # 优先用专门为标题生成配置的 LLM；没配则沿用主对话 LLM
        llm_cfg = config.title_llm or config.llm
        if not (llm_cfg and llm_cfg.model and llm_cfg.api_key):
            return ""

        from ftre_agent_core.llm import LLMHandler, LLMResponse, StreamDelta

        handler = LLMHandler(
            model=llm_cfg.model,
            api_key=llm_cfg.api_key,
            api_base=llm_cfg.api_base,
            api_type=llm_cfg.api_type,
        )
        messages = [
            {"role": "system", "content": self.TITLE_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        chunks: list[str] = []
        for item in handler.stream(messages, tools=None):
            if isinstance(item, StreamDelta) and item.content:
                chunks.append(item.content)
            elif isinstance(item, LLMResponse) and item.content:
                # 极少数实现会一次性把内容塞 LLMResponse.content
                chunks.append(item.content)
        raw = "".join(chunks).strip()
        return self._sanitize_title(raw)

    @classmethod
    def _sanitize_title(cls, raw: str) -> str:
        """清洗 LLM 输出：去引号、去前缀、压缩空白、硬截断。"""
        if not raw:
            return ""
        s = raw.strip()
        # 取第一行 —— 模型偶尔多嘴写好几行
        s = s.splitlines()[0].strip()
        # 去掉外层成对引号 / 反引号 / 中文括号
        # 同字符成对：" ' `
        # 异字符配对：「」 “” ‘’ 《》 【】
        same_pairs = ('"', "'", "`")
        diff_pairs = (("「", "」"), ("“", "”"), ("‘", "’"), ("《", "》"), ("【", "】"))
        for q in same_pairs:
            if len(s) >= 2 and s.startswith(q) and s.endswith(q):
                s = s[1:-1].strip()
                break
        else:
            for left, right in diff_pairs:
                if len(s) >= 2 and s.startswith(left) and s.endswith(right):
                    s = s[1:-1].strip()
                    break
        # 去常见前缀
        for prefix in ("标题：", "标题:", "Title:", "title:"):
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
                break
        # 末尾标点修剪
        s = s.rstrip("。.!?！？,，;；:：")
        # 硬截断
        if len(s) > cls.TITLE_MAX_CHARS:
            s = s[: cls.TITLE_MAX_CHARS]
        return s