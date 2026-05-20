"""
read 工具 - 读取文件内容
"""
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter

from .bash import _BashState


def _resolve(path: str, state: "_BashState | None") -> Path:
    """解析路径：相对路径基于 state.cwd（如果有），绝对路径直接用"""
    p = Path(path).expanduser()
    if not p.is_absolute() and state is not None:
        p = state.cwd / p
    return p.resolve()


def create_read_tool(
    max_bytes: int = 256 * 1024,
    state: "_BashState | None" = None,
) -> Tool:
    """创建 read 工具

    Args:
        max_bytes: 单文件最大读取字节数（默认 256KB）
        state: 共享 cwd 状态（与 bash 共用，相对路径基于此解析）
    """

    def read(path: str, start_line: int = 0, end_line: int = 0) -> str:
        try:
            p = _resolve(path, state)
            if not p.exists():
                return f"[error] 文件不存在: {p}"
            if not p.is_file():
                return f"[error] 不是文件: {p}"

            size = p.stat().st_size
            if size > max_bytes and end_line == 0:
                return (
                    f"[error] 文件过大 ({size} bytes > {max_bytes})。"
                    f"请使用 start_line / end_line 分段读取"
                )

            content = p.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            if start_line > 0 or end_line > 0:
                start = max(0, start_line - 1)
                end = end_line if end_line > 0 else len(lines)
                lines = lines[start:end]
                numbered = [f"{i + start + 1:6d}| {line}" for i, line in enumerate(lines)]
                return "\n".join(numbered)

            numbered = [f"{i + 1:6d}| {line}" for i, line in enumerate(lines)]
            return "\n".join(numbered)
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="read",
        description=(
            "读取文件内容并返回带行号的文本。相对路径基于当前工作目录（由 bash cd 切换）。"
            "可选 start_line / end_line 限定行范围（1-indexed，闭区间）。"
            "对大文件必须指定行范围。"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前 cwd）", required=True),
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
