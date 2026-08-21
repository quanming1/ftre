"""
输出截断工具 — 防止工具返回值过长导致上下文爆炸。

策略：保留头部和尾部各一半，中间插入截断提示。
"""

# 默认最大输出字符数
MAX_OUTPUT_CHARS = 100000

_TRUNCATE_NOTICE = "\n\n... [已截断：输出共 {total} 字符，超过上限 {limit}，中间省略 {omitted} 字符] ...\n\n"


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """
    如果 text 超过 max_chars，保留头尾各约一半，中间插截断提示。
    不超过则原样返回。
    """
    if len(text) <= max_chars:
        return text

    # 预留一点空间给提示文本本身（约 120 字符）
    usable = max_chars - 120
    head_size = usable // 2
    tail_size = usable - head_size

    head = text[:head_size]
    tail = text[-tail_size:]
    omitted = len(text) - head_size - tail_size

    notice = _TRUNCATE_NOTICE.format(
        total=len(text),
        limit=max_chars,
        omitted=omitted,
    )
    return head + notice + tail
