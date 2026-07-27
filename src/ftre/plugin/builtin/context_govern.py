"""
context_govern — 上下文治理插件

通过 before_messages_build hook 注入 Agent 与工作区的 AGENTS.md。

messages 表存储的是已经聚合、校验过的 Msg，不再对流式工具事件做重放治理。
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
        """before_messages_build hook：注入 AGENTS.md。"""
        self._inject_agents_md(ctx)
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
