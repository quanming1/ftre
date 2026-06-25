"""
edit 工具 - 修改已有文件，支持精确字符串替换与按行号替换两种模式
"""
from ftre_agent_core.tool import Tool, ToolParameter, Injected

from .read import _resolve
from ._io import read_text, write_text_preserving
from ._workspace import WorkspaceAccessor


def _line_numbers_of_matches(text: str, needle: str, max_show: int = 5) -> list[int]:
    """返回 needle 在 text 中所有匹配的起始行号（1-indexed），最多 max_show 个"""
    if not needle:
        return []
    lines: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            break
        line_no = text.count("\n", 0, idx) + 1
        lines.append(line_no)
        if len(lines) >= max_show:
            break
        start = idx + 1
    return lines


def _trimmed_match_hint(text: str, needle: str) -> str | None:
    """匹配 0 次时给定位提示：是否缩进/前后空格不一致"""
    if not needle.strip():
        return None
    needle_lines = [ln.strip() for ln in needle.splitlines()]
    if not needle_lines:
        return None
    fingerprint = next((ln for ln in needle_lines if ln), "")
    if not fingerprint:
        return None
    text_lines = text.splitlines()
    hits = [i + 1 for i, ln in enumerate(text_lines) if ln.strip() == fingerprint]
    if hits:
        return (
            f"提示：文件第 {hits[:3]} 行存在 strip 后能匹配的内容，"
            f"很可能是缩进/前后空格/制表符与 old_str 不一致。"
            f"建议先用 read 查看精确字节再 edit。"
        )
    return None


def _format_result(p, old_lines: int, new_lines: int, n: int, tf) -> str:
    return (
        f"已修改 {p} ({old_lines} 行 → {new_lines} 行, "
        f"{n} bytes, encoding={tf.encoding}, newline={tf.newline!r})"
    )


def _edit_by_string(p, content: str, old_str: str, new_norm: str, tf) -> str:
    """字符串模式：old_str 须在文件中唯一匹配后替换。"""
    if not old_str:
        return "[error] 字符串模式需要 old_str；若要按行替换请改用 start_line / end_line"
    old_norm = old_str.replace("\r\n", "\n").replace("\r", "\n")
    count = content.count(old_norm)

    if count == 0:
        hint = _trimmed_match_hint(content, old_norm) or (
            "提示：old_str 必须与文件中的字节完全一致（含缩进/换行）。"
            "建议先用 read 工具读取目标区间，再复制对应文本作为 old_str。"
        )
        return f"[error] 未找到 old_str。{hint}"

    if count > 1:
        line_nos = _line_numbers_of_matches(content, old_norm)
        more = f"... 等 {count} 处" if count > len(line_nos) else ""
        return (
            f"[error] old_str 匹配到 {count} 处（行号 {line_nos}{more}），需要唯一匹配。"
            f"请在 old_str 中加入更多上下文（前后行）以唯一定位。"
        )

    n = write_text_preserving(p, content.replace(old_norm, new_norm, 1), tf)
    return _format_result(p, old_norm.count("\n") + 1, new_norm.count("\n") + 1, n, tf)


def _edit_by_line(p, content: str, new_norm: str, start_line: int, end_line: int, tf) -> str:
    """行号模式：用 new_str 替换 [start_line, end_line] 闭区间内的行（1-indexed）。

    end_line<=0 表示只替换 start_line 单行；new_str 为空串等价于删除该区间。
    行号语义与 read 工具一致，便于先 read 看行号再按号编辑。
    """
    # 保留末尾换行信息：splitlines 会丢掉它，写回时需按原样补回。
    lines = content.splitlines()
    trailing_nl = content.endswith("\n")
    total = len(lines)

    if start_line > total:
        return f"[error] start_line={start_line} 超出文件总行数 {total}"
    end = end_line if end_line > 0 else start_line
    if end < start_line:
        return f"[error] end_line={end_line} 小于 start_line={start_line}"
    end = min(end, total)

    start_idx = start_line - 1
    replacement = new_norm.split("\n") if new_norm != "" else []
    new_lines = lines[:start_idx] + replacement + lines[end:]
    new_content = "\n".join(new_lines)
    if trailing_nl and new_content:
        new_content += "\n"

    n = write_text_preserving(p, new_content, tf)
    return _format_result(p, end - start_idx, len(replacement), n, tf)


def create_edit_tool() -> Tool:
    """创建 edit 工具

    两种模式：
    - 字符串模式（默认）：old_str 在文件中严格唯一匹配后替换，0 次或 >1 次报错并给定位提示。
    - 行号模式：提供 start_line（可选 end_line）按行替换，无需 old_str。
    写回保留原 encoding 与换行风格；AI 传入的换行/编码会被转换为与目标文件一致。
    """

    def edit(
        path: str,
        new_str: str,
        old_str: str = "",
        start_line: int = 0,
        end_line: int = 0,
        ws: WorkspaceAccessor = Injected("workspace"),
    ) -> str:
        try:
            if not isinstance(ws, WorkspaceAccessor):
                return "[error] runtime_context.workspace 未注入"
            cwd = ws.get()
            p = _resolve(path, cwd)
            if not p.exists():
                return f"[error] 文件不存在: {p}"
            if p.is_dir():
                return f"[error] 是目录而非文件: {p}"
            if not p.is_file():
                return f"[error] 不是普通文件: {p}"

            tf = read_text(p)
            content = tf.text
            # 把 AI 传入的换行统一成内部的 \n；编码与最终换行风格由 write_text_preserving
            # 按原文件还原，因此 AI 用什么换行/编码输入都会被转换成与目标文件一致。
            new_norm = new_str.replace("\r\n", "\n").replace("\r", "\n")

            # 行号模式：给了 start_line 就按行替换，不依赖 old_str 精确匹配。
            if start_line > 0:
                return _edit_by_line(p, content, new_norm, start_line, end_line, tf)

            # 字符串模式：old_str 必须唯一匹配。
            return _edit_by_string(p, content, old_str, new_norm, tf)
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="edit",
        description=(
            "修改已有文件，支持两种模式。相对路径基于当前会话的工作区目录。\n"
            "字符串模式（默认，给 old_str）：\n"
            "- old_str 必须在文件中【唯一】匹配（含缩进和换行）\n"
            "- 0 次匹配：会提示是否为缩进/空格不一致\n"
            "- 多次匹配：会列出行号，提示加更多上下文\n"
            "行号模式（给 start_line，可选 end_line）：\n"
            "- 用 new_str 替换 [start_line, end_line] 闭区间内的整行（1-indexed），无需 old_str\n"
            "- 不给 end_line 只替换 start_line 单行；new_str 传空串表示删除这些行\n"
            "- 行号与 read 工具一致，可先 read 看行号再按号编辑\n"
            "通用：\n"
            "- 写回时保留原编码与换行风格（CRLF/LF），new_str 的换行/编码会自动转换为与文件一致\n"
            "- 新建文件请用 write 工具"
        ),
        parameters=[
            ToolParameter(name="path", type="string", description="文件路径（绝对或相对当前工作区）", required=True),
            ToolParameter(
                name="new_str",
                type="string",
                description="替换后的新文本（行号模式传空串表示删除区间）",
                required=True,
            ),
            ToolParameter(
                name="old_str",
                type="string",
                description="字符串模式：要替换的原文（必须在文件中唯一）。行号模式可省略",
                required=False,
            ),
            ToolParameter(
                name="start_line",
                type="number",
                description="行号模式：起始行号（1-indexed）。>0 时启用行号模式",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="行号模式：结束行号（1-indexed，闭区间）。0 表示只替换 start_line 单行",
                required=False,
            ),
        ],
        func=edit,
    )
