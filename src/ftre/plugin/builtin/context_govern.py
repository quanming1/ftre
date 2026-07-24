"""
context_govern — 上下文治理插件

通过 before_messages_build hook 对事件流做处理：
1. tool 事件配对治理：孤立清理 + 悬挂丢弃 + 不相邻修复（tool_result 提到 toolCall 后紧邻位置）
2. 去重 toolCall block + tool_result 事件（同一 id 只保留第一个）

新协议下 tool_call 不再是独立事件，而是嵌在 assistant_message_complete 的
content[] 中（type="toolCall"）。tool_result 是独立事件。
由于 DB 按 timestamp 排序，tool_result 的 timestamp 可能比 toolCall 之间插入的
user_message 更大，导致读取历史时顺序非法。不相邻修复确保 to_openai_messages
产出的消息序列满足 OpenAI 协议。
"""
import logging
import os

from ftre.plugin import Plugin, BEFORE_MESSAGES_BUILD

logger = logging.getLogger(__name__)


class ContextGovernPlugin(Plugin):
    name = "context_govern"
    version = "1.0.0"

    def setup(self) -> None:
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._govern)

    async def _govern(self, ctx):
        """before_messages_build hook：tool 事件配对治理 + 去重 + 顺序修复 + 注入 AGENTS.md"""
        events = ctx.events

        # Step 1: 配对治理 — 孤立清理 + 悬挂丢弃 + 不相邻修复
        events = self._fix_tool_events(events)

        # Step 2: 去重 toolCall block + tool_result 事件（同一 id 只保留第一个）
        events = self._dedup_tool_events(events)

        # Step 3: 注入 AGENTS.md 到系统提示词
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

    # ─── tool 事件配对治理 ─────────────────────────────────────

    def _fix_tool_events(self, events: list) -> list:
        """
        一次性处理 tool_call / tool_result 的配对问题：

        1. 孤立清理：tool_result 无匹配 toolCall → 丢弃；toolCall 无匹配 tool_result → 移除 block
        2. 悬挂丢弃：tool_result 的 toolCall 已被裁剪 → 丢弃
        3. 不相邻修复：toolCall 和 tool_result 之间夹杂了其他事件（如 user_message）
           → 把 tool_result 强行提到 toolCall 后面紧邻位置
        """
        if not events:
            return events

        # ── 收集 call_ids / result_ids ──
        call_ids: set[str] = set()
        result_ids: set[str] = set()
        for ev in events:
            t = ev.get("type")
            if t == "assistant_message_complete":
                data = ev.get("data") or {}
                for block in data.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tid = block.get("id", "")
                        if tid:
                            call_ids.add(tid)
            elif t == "tool_result":
                result_ids.add((ev.get("data") or {}).get("id", ""))

        paired = call_ids & result_ids
        orphan_calls = call_ids - paired
        orphan_results = result_ids - paired

        # ── 单遍处理：丢弃孤立 + 收集待排序的 tool_result ──
        dropped_results = 0
        stripped_calls = 0
        out: list = []

        for ev in events:
            t = ev.get("type")

            if t == "assistant_message_complete":
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

            elif t == "tool_result":
                tid = (ev.get("data") or {}).get("id", "")
                # 孤立/悬挂 tool_result（无匹配 toolCall）→ 丢弃
                if tid in orphan_results:
                    dropped_results += 1
                    continue
                out.append(ev)

            else:
                out.append(ev)

        if dropped_results:
            logger.info(f"[context_govern] 丢弃 {dropped_results} 个孤立/悬挂 tool_result")
        if stripped_calls:
            logger.info(f"[context_govern] 移除 {stripped_calls} 个孤立 toolCall block")

        # ── 不相邻修复：把 tool_result 提到 toolCall 后面紧邻位置 ──
        events = self._fix_tool_result_order(out)
        return events

    def _fix_tool_result_order(self, events: list) -> list:
        """
        确保每个 tool_result 紧跟在发起它的 assistant_message_complete(toolCall) 之后。
        如果中间夹杂了其他事件（如 user_message），把 tool_result 提上去。
        """
        if not events:
            return events

        # 找所有 tool_result 的位置
        result_positions: dict[str, int] = {}
        for i, ev in enumerate(events):
            if ev.get("type") == "tool_result":
                tid = (ev.get("data") or {}).get("id", "")
                if tid and tid not in result_positions:
                    result_positions[tid] = i

        if not result_positions:
            return events

        moved = 0
        used_results: set[str] = set()
        out: list = []

        for i, ev in enumerate(events):
            t = ev.get("type")

            if t == "assistant_message_complete":
                out.append(ev)
                # 提取这个 amc 里的所有 toolCall id
                for block in (ev.get("data") or {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        tid = block.get("id", "")
                        if not tid or tid not in result_positions:
                            continue
                        if tid in used_results:
                            continue
                        result_idx = result_positions[tid]
                        # 如果 tool_result 就在紧邻位置（i+1），不移动
                        if result_idx == i + 1:
                            # 紧邻位置不需要提前，让正常遍历处理
                            continue
                        # tool_result 在更后面，中间夹了其他事件 → 提上来
                        out.append(events[result_idx])
                        used_results.add(tid)
                        moved += 1

            elif t == "tool_result":
                tid = (ev.get("data") or {}).get("id", "")
                if tid in used_results:
                    continue  # 已经被提前了，跳过原位置
                out.append(ev)

            else:
                out.append(ev)

        if moved:
            logger.info(f"[context_govern] 共修复 {moved} 个 tool_result 顺序")

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
