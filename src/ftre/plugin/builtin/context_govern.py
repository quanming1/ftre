"""
context_govern — 上下文治理插件

通过 before_messages_build hook 对事件流做处理：
1. 丢弃孤立的 toolCall / tool_result（配对校验）
2. 去重 toolCall block + tool_result 事件（同一 id 只保留第一个）
3. 丢弃 toolCall 被裁剪后残留的悬挂 tool_result

新协议下 tool_call 不再是独立事件，而是嵌在 assistant_message_complete 的
content[] 中（type="toolCall"）。所有配对逻辑都从这里提取 call id。
新协议下 toolCall 天然紧邻其 tool_result（runner 先 yield amc 再 yield
tool_result），无需相邻性修复。
"""
import logging
import os

from ftre.plugin import Plugin, BEFORE_MESSAGES_BUILD

logger = logging.getLogger(__name__)


def _extract_call_ids(events: list) -> set[str]:
    """从所有 assistant_message_complete 的 content[] 中提取 toolCall id。"""
    call_ids: set[str] = set()
    for ev in events:
        if ev.get("type") == "assistant_message_complete":
            data = ev.get("data") or {}
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    tid = block.get("id", "")
                    if tid:
                        call_ids.add(tid)
    return call_ids


class ContextGovernPlugin(Plugin):
    name = "context_govern"
    version = "1.0.0"

    def setup(self) -> None:
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._govern)

    async def _govern(self, ctx):
        """before_messages_build hook：清理孤立事件 + 去重 + 丢弃悬挂 tool_result + 注入 AGENTS.md"""
        events = ctx.events

        # Step 1: 丢弃孤立的 toolCall / tool_result（OpenAI 协议要求配对）
        events = self._drop_orphan_tool_events(events)

        # Step 2: 去重 toolCall block + tool_result 事件（同一 id 只保留第一个）
        events = self._dedup_tool_events(events)

        # Step 3: 丢弃 toolCall 被裁剪后残留的悬挂 tool_result
        events = self._drop_dangling_tool_results(events)

        # Step 4: 注入 AGENTS.md 到系统提示词
        self._inject_agents_md(ctx)

        ctx.events = events
        return ctx

    # ─── AGENTS.md 注入 ────────────────────────────────────────

    def _inject_agents_md(self, ctx) -> None:
        """读取 AGENTS.md 并注入到 config.system_prompt。

        注入两份（如果都存在，叠加注入）：
        1. agent_dir/AGENTS.md — Agent 行为规则
        2. workspace/AGENTS.md — 项目约定
        """
        injected: list[tuple[str, str]] = []  # [(path, content), ...]

        # 1. agent_dir/AGENTS.md
        agent_dir = (getattr(ctx, "agent_dir", "") or "").strip()
        if agent_dir and os.path.isdir(agent_dir):
            candidate = os.path.join(agent_dir, "AGENTS.md")
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        injected.append((candidate, content))
                except OSError:
                    logger.warning(f"[context_govern] 无法读取 {candidate}")

        # 2. workspace/AGENTS.md
        ws = (getattr(ctx, "workspace", "") or "").strip()
        if ws and os.path.isdir(ws):
            candidate = os.path.join(ws, "AGENTS.md")
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        injected.append((candidate, content))
                except OSError:
                    logger.warning(f"[context_govern] 无法读取 {candidate}")

        if not injected:
            return

        current = (ctx.config.system_prompt or "").strip()
        for path, content in injected:
            current = (
                f"{current}\n\n"
                f'<AGENTS_RULE desc="以下是用户在工作区自定义的规则与指令，你必须严格遵守" path="{path}">\n'
                f"{content}\n"
                f"</AGENTS_RULE>"
            )
            logger.info(f"[context_govern] 已注入 {path} ({len(content)} chars)")

        ctx.config.system_prompt = current

    # ─── 孤立事件清理 ─────────────────────────────────────────

    def _drop_orphan_tool_events(self, events: list) -> list:
        """
        丢弃孤立的 toolCall / tool_result 事件。

        新协议下 toolCall 嵌在 assistant_message_complete 的 content[] 中，
        tool_result 是独立事件。两者必须配对。

        - tool_result 没有 matching toolCall → 丢弃 tool_result
        - toolCall 没有 matching tool_result → 从 content[] 中移除该 block
        """
        if not events:
            return events

        call_ids = _extract_call_ids(events)
        result_ids: set[str] = set()
        for ev in events:
            if ev.get("type") == "tool_result":
                result_ids.add((ev.get("data") or {}).get("id", ""))

        paired = call_ids & result_ids
        orphan_calls = call_ids - paired
        orphan_results = result_ids - paired

        if not orphan_calls and not orphan_results:
            return events

        dropped_results = 0
        stripped_calls = 0
        out: list = []

        for ev in events:
            t = ev.get("type")

            if t == "tool_result":
                tid = (ev.get("data") or {}).get("id", "")
                if tid in orphan_results:
                    dropped_results += 1
                    continue
                out.append(ev)

            elif t == "assistant_message_complete":
                data = ev.get("data") or {}
                content = data.get("content", [])
                if orphan_calls:
                    new_content = []
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "toolCall"
                            and block.get("id", "") in orphan_calls
                        ):
                            stripped_calls += 1
                            continue
                        new_content.append(block)
                    if len(new_content) != len(content):
                        data["content"] = new_content
                out.append(ev)

            else:
                out.append(ev)

        if dropped_results:
            logger.info(f"[context_govern] 丢弃 {dropped_results} 个孤立 tool_result")
        if stripped_calls:
            logger.info(f"[context_govern] 移除 {stripped_calls} 个孤立 toolCall block")
        return out

    def _dedup_tool_events(self, events: list) -> list:
        """
        去重 toolCall block + tool_result 事件（同一 id 只保留第一个）。

        同一 id 可能因 DB 重复写入出现多次：
        - toolCall block 出现在多个 amc 的 content[] 中 → 只保留第一个
        - tool_result 事件出现多条 → 只保留第一条
        """
        if not events:
            return events

        seen_call_ids: set[str] = set()
        seen_result_ids: set[str] = set()
        dropped_calls = 0
        dropped_results = 0
        out: list = []

        for ev in events:
            t = ev.get("type")

            if t == "assistant_message_complete":
                data = ev.get("data") or {}
                content = data.get("content", [])
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tid = block.get("id", "")
                        if tid and tid in seen_call_ids:
                            dropped_calls += 1
                            continue
                        if tid:
                            seen_call_ids.add(tid)
                    new_content.append(block)
                if len(new_content) != len(content):
                    data["content"] = new_content
                out.append(ev)

            elif t == "tool_result":
                tid = (ev.get("data") or {}).get("id", "")
                if tid and tid in seen_result_ids:
                    dropped_results += 1
                    continue
                if tid:
                    seen_result_ids.add(tid)
                out.append(ev)

            else:
                out.append(ev)

        if dropped_calls:
            logger.info(f"[context_govern] 移除 {dropped_calls} 个重复 toolCall block")
        if dropped_results:
            logger.info(f"[context_govern] 移除 {dropped_results} 个重复 tool_result")
        return out

    def _drop_dangling_tool_results(self, events: list) -> list:
        """
        丢弃 toolCall 尚未出现就被引用到的 tool_result。

        压缩裁剪后，events 开头可能出现 tool_result 而其 toolCall 所在的
        assistant_message_complete 已被裁掉。这会导致 to_openai_messages 产出
        的第一条消息带悬挂 tool_call_id，LLM 报错。
        """
        if not events:
            return events

        seen_call_ids: set[str] = set()
        dropped = 0
        out: list = []

        for ev in events:
            t = ev.get("type")
            data = ev.get("data") or {}

            if t == "assistant_message_complete":
                # 记录此事件中所有 toolCall id
                for block in data.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tid = block.get("id", "")
                        if tid:
                            seen_call_ids.add(tid)
                out.append(ev)

            elif t == "tool_result":
                tid = data.get("id", "")
                if not tid or tid in seen_call_ids:
                    out.append(ev)
                else:
                    dropped += 1
                    logger.debug(
                        f"[context_govern] 丢弃悬挂 tool_result id={tid}: "
                        f"toolCall 已被裁剪或尚未出现"
                    )

            else:
                out.append(ev)

        if dropped:
            logger.info(f"[context_govern] 丢弃 {dropped} 个悬挂 tool_result")
        return out
