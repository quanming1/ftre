"""Feature Plugin that injects workspace AGENTS.md governance rules."""

from __future__ import annotations

from pathlib import Path

from cordis import PluginContext
from ftre.services.system_prompt.types import PromptSection

inject = ("system_prompt", "workspaces", "filesystem")
provide = ()


def apply(ctx: PluginContext, config=None):
    """Register a lazy prompt section so the current workspace is read per turn."""
    def workspace_rules(values):
        workspace = values.get("workspace") or ""
        path = Path(workspace) / "AGENTS.md" if workspace else None
        if path:
            try:
                target = ctx.filesystem.resolve(path)
                info = ctx.filesystem.stat(target)
                if info["kind"] == "file":
                    content = ctx.filesystem.read_text(target, limit=200_000).strip()
                    return f'<AGENTS_RULE path="{target.path}">\n{content}\n</AGENTS_RULE>'
            except (OSError, ValueError, PermissionError):
                return ""
        return ""

    disposer = ctx.system_prompt.register_section(PromptSection(name="workspace-rules", factory=workspace_rules, priority=40, owner="context-govern", source="builtin"))
    ctx.effect(disposer, label="prompt:context-govern")
