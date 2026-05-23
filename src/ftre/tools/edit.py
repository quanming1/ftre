"""
edit 工具 - 通过精确字符串替换修改文件
"""
from ftre_agent_core.tool import Tool, ToolParameter

from .bash import _BashState
from .read import _resolve
from ._io import read_text, write_text_preserving


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
    """
    当 needle 完全匹配为 0 时，尝试帮助 agent 定位：
    - 把 needle 每行 strip 后再去 text 里找
    - 如果能找到，提示是缩进/前后空格不一致
    """
    if not needle.strip():
        return None
    needle_lines = [ln.strip() for ln in needle.splitlines()]
    if not needle_lines:
        return None
    # 取第一行非空作为指纹
    fingerprint = next((ln for ln in needle_lines if ln), "")
    if not fingerprint:
        return None
    # 在 text 中查找首行指纹的次数
    text_lines = text.splitlines()
    hits = [i + 1 for i, ln in enumerate(text_lines) if ln.strip() == fingerprint]
    if hits:
        return (
            f"提示：文件第 {hits[:3]} 行存在 strip 后能匹配的内容，"
            f"很可能是缩进/前后空格/制表符与 old_str 不一致。"
            f"建议先用 read 查看精确字节再 edit。"
        )
    return None


def create_edit_tool(state: "_BashState | None" = None) -> Tool:
    """创建 edit 工具（基于精确字符串替换）

    行为：
    - 严格唯一匹配：0 次或 >1 次都会报错并给定位提示
    - 写回保留原 encoding 与换行风格

    Args:
        state: 共享 cwd 状态（相对路径基于此解析）
    """

    def edit(path: str, old_str: str, new_str: str) -> str:
        try:
            p = _resolve(path, state)
            if not p.exists():
                return f"[error] 文件不存在: {p}"
            if p.is_dir():
                return f"[error] 是目录而非文件: {p}"
            if not p.is_file():
                return f"[error] 不是普通文件: {p}"

            tf = read_text(p)
            content = tf.text  # 已统一为 \n
            # 把 old_str / new_str 的换行也统一为 \n，避免 CRLF 不一致导致匹配失败
            old_norm = old_str.replace("\r\n", "\n").replace("\r", "\n")
            new_norm = new_str.replace("\r\n", "\n").replace("\r", "\n")

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

            new_content = content.replace(old_norm, new_norm, 1)
            n = write_text_preserving(p, new_content, tf)

            old_lines = old_norm.count("\n") + 1
            new_lines = new_norm.count("\n") + 1
            return (
                f"已修改 {p} ({old_lines} 行 → {new_lines} 行, "
                f"{n} bytes, encoding={tf.encoding}, newline={tf.newline!r})"
            )
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="edit",
        description=(
            "通过精确字符串替换修改文件。相对路径基于当前工作目录（由 bash cd 切换）。\n"
            "- old_str 必须在文件中【唯一】匹配（含缩进和换行）\n"
            "- 0 次匹配：会提示是否为缩进/空格不一致\n"
            "- 多次匹配：会列出行号，提示加更多上下文\n"
            "- 写回时保留原编码与换行风格（CRLF/LF）\n"
            "- 新建文件请用 write 工具"
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
