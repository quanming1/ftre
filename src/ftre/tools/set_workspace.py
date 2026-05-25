"""
set_workspace 工具 - 切换当前工作区根目录

工作区由 runtime_context['workspace'] 承载（{'cwd': str} 形式）。
工具通过 Injected 拿到这个 dict 引用并原地改 cwd，
后续 read / write / edit / bash 立即看到新值。

入参约束：必须是绝对路径，且必须是已存在的目录。
"""
import os
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected


def create_set_workspace_tool() -> Tool:
    """创建 set_workspace 工具"""

    def set_workspace(path: str, ws: dict = Injected("workspace")) -> str:
        if not isinstance(ws, dict) or "cwd" not in ws:
            return "[error] runtime_context.workspace 未注入"
        if not path or not path.strip():
            return "[error] path 不能为空"

        target = path.strip().strip('"').strip("'")
        # 仅做环境变量 / ~ 展开，不再用 ws.cwd 拼接相对路径
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

        old = ws["cwd"]
        ws["cwd"] = str(new_dir)
        if old == str(new_dir):
            return f"工作区未变化: {new_dir}"
        return f"工作区已切换: {old} → {new_dir}"

    return Tool(
        name="set_workspace",
        description=(
            "切换当前工作区根目录（会话级）。后续 read / write / edit / bash 的"
            "相对路径都基于这个目录解析。\n"
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
