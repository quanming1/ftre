"""
set_workspace 工具 - 切换当前会话的工作区根目录

工作区是 sessions 表的一等字段，本工具直接读写 DB。后续 read / write /
edit / bash 立刻看到新值（它们都通过同一个 WorkspaceAccessor 取 cwd）。

入参约束：必须是绝对路径，且必须是已存在的目录。
"""
import os
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected

from ._workspace import WorkspaceAccessor


def create_set_workspace_tool() -> Tool:
    """创建 set_workspace 工具"""

    def set_workspace(path: str, ws: WorkspaceAccessor = Injected("workspace")) -> str:
        if not isinstance(ws, WorkspaceAccessor):
            return "[error] runtime_context.workspace 未注入"
        if not path or not path.strip():
            return "[error] path 不能为空"

        target = path.strip().strip('"').strip("'")
        # 仅做环境变量 / ~ 展开，不允许相对路径（ws.get() 不该被当成基准目录）
        target = os.path.expandvars(os.path.expanduser(target))
        new_dir = Path(target)

        if not new_dir.is_absolute():
            return f"[error] path 必须是绝对路径，收到: {path}"

        try:
            new_dir = new_dir.resolve()
        except OSError as e:
            return f"[error] 无法解析路径: {e}"
        if not new_dir.exists():
            return f"[error] 目录不存在: {new_dir}"
        if not new_dir.is_dir():
            return f"[error] 不是目录: {new_dir}"

        new_str = str(new_dir)
        old = ws.set(new_str)
        if old == new_str:
            return f"<FTRE_SYSTEM_FACT>[workspace] {new_dir}（未变化）</FTRE_SYSTEM_FACT>"
        return f"<FTRE_SYSTEM_FACT>[workspace] {old} → {new_dir}</FTRE_SYSTEM_FACT>"

    return Tool(
        name="set_workspace",
        description=(
            "切换当前会话的工作区根目录（持久到 DB）。后续 read / write / edit / "
            "bash 的相对路径都基于这个目录解析。\n"
            "- path 必须是【绝对路径】，相对路径会被拒绝\n"
            "- 支持 ~ 和环境变量展开（展开后必须是绝对路径）\n"
            "- 目标必须是已存在的目录，否则报错\n"
            "- 适合在长任务开始时一次性声明工作区，避免 bash cd 来回切换"
        ),
        parameters=[
            ToolParameter(
                name="path",
                type="string",
                description="目标工作区目录（必须为绝对路径）",
                required=True,
            ),
        ],
        func=set_workspace,
    )
