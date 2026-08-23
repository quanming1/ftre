"""Feature Plugin that injects workspace AGENTS.md governance rules."""
# 治理 Plugin：从 WorkspaceService 解析当前工作区，读取其中的 AGENTS.md，
# 以可撤销的 prompt section 注册到 SystemPromptService。
# 采用"懒工厂"（factory）而非固定内容：每次拼 prompt 时按当时的
# workspace 取值重新读取文件，保证切换工作区后规则自动跟随。

from __future__ import annotations

from pathlib import Path

from cordis import Context

from ftre.services.system_prompt.types import PromptSection

inject = ("system_prompt", "workspaces", "filesystem")
provide = ()


def apply(ctx: Context, config=None):
    """Register a lazy prompt section so the current workspace is read per turn."""
    # 懒工厂：values 由 SystemPromptService 拼装 prompt 时提供，含 workspace 键。
    def workspace_rules(values):
        workspace = values.get("workspace") or ""
        path = Path(workspace) / "AGENTS.md" if workspace else None
        if path:
            try:
                # 通过 filesystem 服务做路径解析与读取，不直接碰磁盘 API
                target = ctx.filesystem.resolve(path)
                info = ctx.filesystem.stat(target)
                if info["kind"] == "file":
                    # 上限 200KB，防止超大治理文件撑爆 prompt
                    content = ctx.filesystem.read_text(target, limit=200_000).strip()
                    return f'<AGENTS_RULE path="{target.path}">\n{content}\n</AGENTS_RULE>'
            except (OSError, ValueError, PermissionError):
                # 读取失败时静默返回空，不让治理规则阻断正常对话
                return ""
        return ""

    # priority=40：比系统提示词基础段低、比 plan/skill 说明高，处于中等优先级
    disposer = ctx.system_prompt.register_section(PromptSection(name="workspace-rules", factory=workspace_rules, priority=40, owner="context-govern", source="builtin"))
    # 注册可逆清理：Fiber unload 时摘除该 section
    ctx.effect(lambda: disposer, label="prompt:context-govern")
