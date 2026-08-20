from __future__ import annotations

from pathlib import Path

from cordis import PluginContext
from ftre.services.system_prompt.types import PromptSection

inject = ("system_prompt", "workspaces")
provide = ()


def apply(ctx: PluginContext, config=None):
    def workspace_rules(values):
        workspace = values.get("workspace") or ""
        path = Path(workspace) / "AGENTS.md" if workspace else None
        if path and path.is_file():
            return f'<AGENTS_RULE path="{path}">\n{path.read_text(encoding="utf-8", errors="replace").strip()}\n</AGENTS_RULE>'
        return ""

    disposer = ctx.system_prompt.register_section(PromptSection(name="workspace-rules", factory=workspace_rules, priority=40, owner="context-govern", source="builtin"))
    ctx.effect(disposer, label="prompt:context-govern")

