"""
read 工具 - 读取文件内容
"""
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected

from ._io import read_text


def _resolve(path: str, ws: dict) -> Path:
    """解析路径：相对路径基于 ws['cwd']，绝对路径直接用"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(ws["cwd"]) / p
    return p.resolve()


def create_read_tool(max_bytes: int = 256 * 1024) -> Tool:
    """创建 read 工具

    Args:
        max_bytes: 单文件最大读取字节数（默认 256KB）。超出必须显式 start_line/end_line
    """

    def read(
        path: str,
        start_line: int = 0,
        end_line: int = 0,
        ws: dict = Injected("workspace"),
    ) -> str:
        try:
            if not isinstance(ws, dict) or "cwd" not in ws:
                return "[error] runtime_context.workspace 未注入"
            p = _resolve(path, ws)
            if not p.exists():
                return f"[error] 文件不存在: {p}"
            if p.is_dir():
                return (
                    f"[error] 是目录而非文件: {p}\n"
                    f"提示：用 bash 工具运行 `ls`/`dir` 列出目录内容"
                )
            if not p.is_file():
                return f"[error] 不是普通文件: {p}"

            size = p.stat().st_size
            if size > max_bytes and end_line == 0:
                return (
                    f"[error] 文件过大 ({size} bytes > {max_bytes})。"
                    f"请使用 start_line / end_line 分段读取"
                )

            tf = read_text(p)
            lines = tf.text.splitlines()

            if start_line > 0 or end_line > 0:
                start = max(0, start_line - 1)
                end = end_line if end_line > 0 else len(lines)
                visible = lines[start:end]
                numbered = [f"{i + start + 1:6d}| {line}" for i, line in enumerate(visible)]
            else:
                numbered = [f"{i + 1:6d}| {line}" for i, line in enumerate(lines)]

            header = ""
            if tf.encoding != "utf-8" and tf.encoding != "utf-8-sig":
                header = f"[encoding] {tf.encoding}\n"
            return header + "\n".join(numbered)
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="read",
        description=(
            "读取文件内容并返回带行号的文本。相对路径基于当前工作区目录。\n"
            "- 自动识别编码（utf-8 / utf-8-sig / gbk 等），非 utf-8 文件首行会显示 [encoding]\n"
            "- 可选 start_line / end_line 限定行范围（1-indexed，闭区间）\n"
            "- 超过 256KB 的文件必须传入行范围\n"
            "- 路径是目录时拒绝读取，提示用 bash 列目录"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前工作区）", required=True),
            ToolParameter(
                name="start_line",
                type="number",
                description="起始行号（1-indexed），0 表示从头",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="结束行号（1-indexed，闭区间），0 表示到末尾",
                required=False,
            ),
        ],
        func=read,
    )
