"""
edit 工具 - 通过精确字符串替换修改文件
"""
from ftre_agent_core.tool import Tool, ToolParameter

from .bash import _BashState
from .read import _resolve


def create_edit_tool(state: "_BashState | None" = None) -> Tool:
    """创建 edit 工具（基于精确字符串替换）

    Args:
        state: 共享 cwd 状态（相对路径基于此解析）
    """

    def edit(path: str, old_str: str, new_str: str) -> str:
        try:
            p = _resolve(path, state)
            if not p.exists():
                return f"[error] 文件不存在: {p}"
            if not p.is_file():
                return f"[error] 不是文件: {p}"

            content = p.read_text(encoding="utf-8")
            count = content.count(old_str)

            if count == 0:
                return f"[error] 未找到 old_str。请确保完全匹配（包括缩进和换行）"
            if count > 1:
                return (
                    f"[error] old_str 匹配到 {count} 处，需要唯一。"
                    f"请提供更多上下文以唯一定位"
                )

            new_content = content.replace(old_str, new_str, 1)
            p.write_text(new_content, encoding="utf-8")

            old_lines = old_str.count("\n") + 1
            new_lines = new_str.count("\n") + 1
            return f"已修改 {p} ({old_lines} 行 → {new_lines} 行)"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="edit",
        description=(
            "通过精确字符串替换修改文件。相对路径基于当前工作目录（由 bash cd 切换）。"
            "old_str 必须在文件中唯一匹配（包含缩进和换行）。"
            "新文件创建请用 write 工具。"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前 cwd）", required=True),
            ToolParameter(
                name="old_str",
                type="string",
                description="要替换的原文（必须在文件中唯一）",
                required=True,
            ),
            ToolParameter(
                name="new_str",
                type="string",
                description="替换后的新文本",
                required=True,
            ),
        ],
        func=edit,
    )
