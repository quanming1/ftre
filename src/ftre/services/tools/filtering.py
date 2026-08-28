"""Agent profile 的 tools.allow / tools.deny 过滤逻辑。

由 ToolService Owner 在构建 view 时使用；形状校验宁可显式失败，
也不能让畸形配置静默清空成员的全部工具。
"""

from __future__ import annotations


def coerce_tool_name_list(value, field: str) -> list[str]:
    """tools.allow / tools.deny 规范化。

    None → []；单个字符串宽容为单元素列表（allow="bash" 语义即"只放行 bash"）；
    其余必须是字符串列表，否则抛 ValueError——宁可显式失败，
    也不能让畸形配置静默清空成员的全部工具。
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(  # noqa: TRY004 legacy compatibility boundary reviewed in F1
            f"tools.{field} 必须是字符串列表，实际: {type(value).__name__}"
        )
    bad = [x for x in value if not isinstance(x, str) or not x.strip()]
    if bad:
        raise ValueError(f"tools.{field} 含非字符串或空元素: {bad!r}")
    return value
