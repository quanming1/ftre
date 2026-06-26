"""
write 工具 - 写入/覆盖文件
"""
from ftre_agent_core.tool import Tool, ToolParameter, Injected

from .read import _resolve
from ._io import read_text, write_text_new, write_text_preserving, _NEWLINE_LABEL
from ._workspace import WorkspaceAccessor


def create_write_tool() -> Tool:
    """创建 write 工具（覆盖写入整个文件）

    行为：
    - 已存在文件：保留原 encoding 和换行风格（CRLF/LF）
    - 新文件：utf-8 + LF
    """

    def write(path: str, content: str, ws: WorkspaceAccessor = Injected("workspace")) -> str:
        try:
            if not isinstance(ws, WorkspaceAccessor):
                return "[error] runtime_context.workspace 未注入"
            cwd = ws.get()
            p = _resolve(path, cwd)
            if p.exists() and p.is_dir():
                return f"[error] 目标路径是目录: {p}"

            p.parent.mkdir(parents=True, exist_ok=True)
            normalized = content.replace("\r\n", "\n").replace("\r", "\n")

            if p.exists():
                original = read_text(p)
                n = write_text_preserving(p, normalized, original)
                encoding, newline = original.encoding, original.newline
                action = "已覆盖"
            else:
                n = write_text_new(p, normalized, encoding="utf-8", newline="\n")
                encoding, newline = "utf-8", "\n"
                action = "已创建"

            newline_label = _NEWLINE_LABEL.get(newline, repr(newline))
            return (
                "<FTRE_SYSTEM_FACT>\n"
                f"[file] {p}\n"
                f"[meta] encoding={encoding} newline={newline_label} size={n}bytes\n"
                f"{action}\n"
                "</FTRE_SYSTEM_FACT>"
            )
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="write",
        description=(
            "创建新文件或覆盖现有文件。相对路径基于当前会话的工作区目录。\n"
            "- 父目录不存在会自动创建\n"
            "- 覆盖现有文件时保留原编码和换行风格（CRLF/LF）\n"
            "- 新建文件用 utf-8 + LF\n"
            "- 修改现有文件建议用 edit（保留未改动部分），write 适合整文件替换"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前工作区）", required=True),
            ToolParameter(name="content", type="string", description="文件完整内容", required=True),
        ],
        func=write,
    )
