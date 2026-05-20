"""
write 工具 - 写入/覆盖文件
"""
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter

from .bash import _BashState
from .read import _resolve


def create_write_tool(state: "_BashState | None" = None) -> Tool:
    """创建 write 工具（覆盖写入整个文件）

    Args:
        state: 共享 cwd 状态（相对路径基于此解析）
    """

    def write(path: str, content: str) -> str:
        try:
            p = _resolve(path, state)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"已写入 {p} ({len(content)} chars)"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="write",
        description=(
            "创建新文件或覆盖现有文件。相对路径基于当前工作目录（由 bash cd 切换）。"
            "如果父目录不存在会自动创建。"
            "对已存在文件的修改建议使用 edit 工具（保留未改动部分）。"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前 cwd）", required=True),
            ToolParameter(name="content", type="string", description="文件完整内容", required=True),
        ],
        func=write,
    )
