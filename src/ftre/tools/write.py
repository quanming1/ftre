"""
write 工具 - 写入/覆盖文件
"""
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter

from .bash import _BashState
from .read import _resolve
from ._io import read_text, write_text_new, write_text_preserving


def create_write_tool(state: "_BashState | None" = None) -> Tool:
    """创建 write 工具（覆盖写入整个文件）

    行为：
    - 已存在文件：保留原 encoding 和换行风格（CRLF/LF）
    - 新文件：utf-8 + LF

    Args:
        state: 共享 cwd 状态（相对路径基于此解析）
    """

    def write(path: str, content: str) -> str:
        try:
            p = _resolve(path, state)
            if p.exists() and p.is_dir():
                return f"[error] 目标路径是目录: {p}"

            p.parent.mkdir(parents=True, exist_ok=True)

            if p.exists():
                # 保留原 encoding / 换行风格
                original = read_text(p)
                # 把传入 content 的换行统一成 \n（write_text_preserving 内部会还原）
                normalized = content.replace("\r\n", "\n").replace("\r", "\n")
                n = write_text_preserving(p, normalized, original)
                return f"已覆盖 {p} ({n} bytes, encoding={original.encoding}, newline={original.newline!r})"

            # 新文件：默认 utf-8 + LF
            normalized = content.replace("\r\n", "\n").replace("\r", "\n")
            n = write_text_new(p, normalized, encoding="utf-8", newline="\n")
            return f"已创建 {p} ({n} bytes, encoding=utf-8, newline=LF)"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="write",
        description=(
            "创建新文件或覆盖现有文件。相对路径基于当前工作目录（由 bash cd 切换）。\n"
            "- 父目录不存在会自动创建\n"
            "- 覆盖现有文件时保留原编码和换行风格（CRLF/LF）\n"
            "- 新建文件用 utf-8 + LF\n"
            "- 修改现有文件建议用 edit（保留未改动部分），write 适合整文件替换"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前 cwd）", required=True),
            ToolParameter(name="content", type="string", description="文件完整内容", required=True),
        ],
        func=write,
    )
