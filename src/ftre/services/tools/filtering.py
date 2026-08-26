"""Agent profile 的 tools.allow / tools.deny 过滤逻辑。

由 ToolService Owner 在构建 view 时使用；形状校验宁可显式失败，
也不能让畸形配置静默清空成员的全部工具。
"""

from __future__ import annotations

from typing import Any

from ftre_agent_core.tool import ToolRegistry


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


def filter_tools(registry: ToolRegistry, tools_config: dict[str, Any] | None) -> ToolRegistry:
    """按 agent 的 tools.allow / tools.deny 在 registry 上原地过滤。

    Args:
        registry: 已注册所有工具的 ToolRegistry
        tools_config: agent.config.json 的 tools 字段，格式为
                      {"allow": [...], "deny": [...]} 或 None

    Returns:
        过滤后的同一个 registry（原地修改）。
        tools_config 为 None 时不做任何操作。

    Raises:
        ValueError: allow/deny 形状非法（防止静默清空工具）。

    语义（F34 冻结）：allow/deny 不豁免任何来源的工具——内置工具
    （core-tools 贡献）与 Plugin/MCP 工具同一待遇，因为它们都是普通贡献。
    """
    if not tools_config:
        return registry

    allow = set(coerce_tool_name_list(tools_config.get("allow"), "allow"))
    deny = set(coerce_tool_name_list(tools_config.get("deny"), "deny"))

    for name in list(registry.names):
        if name in deny or allow and name not in allow:
            registry.unregister(name)

    return registry
