"""
context_govern — 上下文治理插件

通过 before_messages_build hook 对事件流做处理：
1. 丢弃孤立的 tool_call / tool_result（配对校验）
2. 修复 tool_call / tool_result 相邻性（OpenAI 协议要求）
3. 丢弃 tool_call 被裁剪后残留的悬挂 tool_result
"""
import logging

from ftre.plugin import Plugin, BEFORE_MESSAGES_BUILD

logger = logging.getLogger(__name__)


class ContextGovernPlugin(Plugin):
    name = "context_govern"
    version = "1.0.0"

    def setup(self) -> None:
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._govern)

    def _govern(self, ctx):
        """before_messages_build hook：清理孤立事件 + 修复相邻性 + 丢弃悬挂 tool_result + 注入 AGENTS.md + 注入用户自定义提示词"""
        events = ctx.events

        # Step 1: 丢弃孤立的 tool_call / tool_result（OpenAI 协议要求配对）
        events = self._drop_orphan_tool_events(events)

        # Step 1.5: 去重 tool_call（同一 id 只保留第一个）
        events = self._dedup_tool_calls(events)

        # Step 2: 修复 tool_call / tool_result 相邻性
        events = self._ensure_adjacency(events)

        # Step 3: 丢弃 tool_call 被裁剪后残留的悬挂 tool_result
        events = self._drop_dangling_tool_results(events)

        # Step 4: 注入工作区下的 AGENTS.md 到系统提示词
        self._inject_agents_md(ctx)

        # Step 5: 注入用户在客户端设置的自定义提示词
        self._inject_user_prompt(ctx)

        ctx.events = events
        return ctx

    # ─── 用户自定义提示词注入 ──────────────────────────────────

    def _inject_user_prompt(self, ctx) -> None:
        """把用户在客户端设置的 user_prompt（config.json）注入到 system_prompt。"""
        user_prompt = (getattr(ctx.config, "user_prompt", "") or "").strip()
        if not user_prompt:
            return

        current = (ctx.config.system_prompt or "").strip()
        ctx.config.system_prompt = (
            f"""{current}

<USER_CUSTOM_PROMPT desc="以下是用户在客户端设置的自定义提示词，代表用户的个人偏好与额外要求，请遵守">
{user_prompt}
</USER_CUSTOM_PROMPT>"""
        )
        logger.info(f"[context_govern] 已注入用户自定义提示词 ({len(user_prompt)} chars)")

    # ─── AGENTS.md 注入 ────────────────────────────────────────

    def _inject_agents_md(self, ctx) -> None:
        """读取 AGENTS.md 并注入到 config.system_prompt。

        优先级：agent_dir/AGENTS.md > workspace/AGENTS.md。
        """
        import os

        # 优先从 agent_dir 读取
        agent_dir = (getattr(ctx, "agent_dir", "") or "").strip()
        agents_path = ""

        if agent_dir and os.path.isdir(agent_dir):
            candidate = os.path.join(agent_dir, "AGENTS.md")
            if os.path.isfile(candidate):
                agents_path = candidate

        # agent_dir 没有 → 回退 workspace
        if not agents_path:
            ws = (ctx.workspace or "").strip()
            if ws and os.path.isdir(ws):
                candidate = os.path.join(ws, "AGENTS.md")
                if os.path.isfile(candidate):
                    agents_path = candidate

        if not agents_path:
            return

        try:
            content = open(agents_path, encoding="utf-8").read().strip()
        except OSError:
            logger.warning(f"[context_govern] 无法读取 {agents_path}")
            return

        if not content:
            return

        current = (ctx.config.system_prompt or "").strip()
        ctx.config.system_prompt = (
            f"""{current}

<AGENTS_RULE desc="以下是用户在工作区自定义的规则与指令，你必须严格遵守" path="{agents_path}">
{content}
</AGENTS_RULE>"""
        )
        logger.info(f"[context_govern] 已注入 {agents_path} ({len(content)} chars)")

    # ─── 孤立事件清理 ─────────────────────────────────────────

    def _drop_orphan_tool_events(self, events: list) -> list:
        """
        丢弃孤立的 tool_call / tool_result 事件。

        OpenAI 协议要求 tool_calls 必须有对应 tool_result 配对，反之亦然。
        孤立事件（cancelled / timed_out / 崩溃残留）会导致 LLM 400 报错。
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
            logger.info(f"[context_govern] 丢弃 {dropped} 个孤立 tool 事件")
        return out

    def _dedup_tool_calls(self, events: list) -> list:
        """
        去重 tool_call 事件（同一 id 只保留第一个）。

        DB 中可能存了重复的 tool_call 事件，导致 to_openai_messages 把
        同一个 id 加到 assistant message 的 tool_calls 数组里两次，
        LLM 报 "Duplicate value for 'tool_call_id'"。
        """
        if not events:
            return events

        seen_ids: set[str] = set()
        out: list = []
        dropped = 0
        for ev in events:
            t = ev.get("type")
            if t == "tool_call":
                tid = (ev.get("data") or {}).get("id", "")
                if tid and tid in seen_ids:
                    dropped += 1
                    continue
                if tid:
                    seen_ids.add(tid)
            out.append(ev)

        if dropped:
            logger.info(f"[context_govern] 丢弃 {dropped} 个重复 tool_call")
        return out

    def _drop_dangling_tool_results(self, events: list) -> list:
        """
        丢弃 tool_call 尚未出现就被引用到的 tool_result。

        压缩裁剪后，events 开头可能出现 tool_result 而其 tool_call 已被裁掉。
        这会导致 to_openai_messages 产出的第一条消息带悬挂 tool_call_id，
        LLM 报 ``unexpected tool_use_id found in tool_result blocks``。
        """
        if not events:
            return events

        seen_call_ids: set[str] = set()
        dropped = 0
        out: list = []
        for ev in events:
            t = ev.get("type")
            tid = (ev.get("data") or {}).get("id", "")
            if t == "tool_call":
                if tid:
                    seen_call_ids.add(tid)
                out.append(ev)
            elif t == "tool_result":
                if not tid or tid in seen_call_ids:
                    out.append(ev)
                else:
                    dropped += 1
                    logger.debug(
                        f"[context_govern] 丢弃悬挂 tool_result id={tid}: "
                        f"tool_call 已被裁剪或尚未出现"
                    )
            else:
                out.append(ev)

        if dropped:
            logger.info(f"[context_govern] 丢弃 {dropped} 个悬挂 tool_result")
        return out

    # ─── 相邻性修复 ─────────────────────────────────────────

    def _ensure_adjacency(self, events: list) -> list:
        """
        保证每个 tool_call 紧跟着对应的 tool_result。

        tool_call 之后被 external_message 等事件打断时，把 tool_result 移到
        紧贴 tool_call 之后。没有打断时返回原列表（零拷贝）。
        重复的 tool_result（同一 tool_call_id 多条）会被去重。
        """
        if not events:
            return events

        call_idx_by_id: dict[str, int] = {}
        pairs: list[tuple[int, int]] = []
        seen_result_ids: set[str] = set()  # 记录已处理的 tool_result id
        duplicate_result_indices: set[int] = set()  # 重复 tool_result 的位置
        for i, ev in enumerate(events):
            t = ev.get("type")
            data = ev.get("data") or {}
            tid = data.get("id", "")
            if not tid:
                continue
            if t == "tool_call":
                call_idx_by_id[tid] = i
            elif t == "tool_result":
                if tid in seen_result_ids:
                    # 重复的 tool_result，标记丢弃
                    duplicate_result_indices.add(i)
                elif tid in call_idx_by_id:
                    seen_result_ids.add(tid)
                    call_i = call_idx_by_id.pop(tid)
                    if i != call_i + 1:
                        pairs.append((call_i, i))

        if not pairs and not duplicate_result_indices:
            return events

        result_indices_to_skip = {result_i for _, result_i in pairs}
        result_indices_to_skip |= duplicate_result_indices
        moves: dict[int, int] = {call_i: result_i for call_i, result_i in pairs}

        out: list = []
        for i, ev in enumerate(events):
            if i in result_indices_to_skip:
                continue
            out.append(ev)
            if i in moves:
                out.append(events[moves[i]])

        fixed = len(pairs)
        dropped_dup = len(duplicate_result_indices)
        if fixed:
            logger.info(f"[context_govern] 修复 {fixed} 对不相邻的 tool_call/tool_result")
        if dropped_dup:
            logger.info(f"[context_govern] 丢弃 {dropped_dup} 个重复 tool_result")
        return out
