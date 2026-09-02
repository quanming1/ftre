"""
MCP 配置解析

从 ~/.ftre/config.json 的 "mcp" 段读取 MCP 服务器配置。

配置格式（与 OpenCode 兼容）：
{
  "mcp": {
    "filesystem": {
      "type": "local",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "environment": { "KEY": "VALUE" },
      "disabled": false,
      "timeout": 30000
    },
    "remote-server": {
      "type": "remote",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer xxx" },
      "disabled": false,
      "timeout": 30000
    }
  }
}

支持 local（stdio）和 remote（streamable HTTP）类型。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class McpServerConfig:
    """单个 MCP 服务器配置"""

    name: str
    type: str  # "local" | "remote"
    # local 专用
    command: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    # remote 专用
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # 通用
    disabled: bool = False
    timeout: int = 30_000  # ms

    @classmethod
    def from_raw(cls, name: str, raw: dict) -> McpServerConfig | None:
        """从单个 server 的 raw dict 构造配置。

        Args:
            name: 服务器名称（mcp 字典的 key）
            raw: 服务器配置内容（mcp 字典的 value）

        Returns:
            McpServerConfig 或 None（disabled / 格式错误时）
        """
        inspection = inspect_mcp_server(name, raw)
        if inspection.error:
            logger.warning("[mcp-config] 跳过 %s：%s", name, inspection.error)
            return None
        return inspection.config


@dataclass(frozen=True)
class McpConfigInspection:
    """One source entry's parse result used by runtime and catalog alike."""

    name: str
    config: McpServerConfig | None
    disabled: bool
    error: str | None = None


def inspect_mcp_server(name: str, raw: Any) -> McpConfigInspection:
    """Validate one raw entry without silently dropping it from diagnostics.

    ``parse_mcp_config`` still returns executable entries only; catalog code uses
    this richer result to render disabled and invalid entries instead of making
    them vanish from the management UI.
    """
    if not isinstance(name, str) or not name:
        return McpConfigInspection(str(name), None, False, "服务器名称不能为空")
    if not isinstance(raw, dict):
        return McpConfigInspection(name, None, False, "配置必须是对象")

    disabled = bool(raw.get("disabled", False) or raw.get("enabled", True) is False)
    server_type = raw.get("type", "")
    if server_type not in ("local", "remote"):
        if "command" in raw:
            server_type = "local"
        else:
            return McpConfigInspection(
                name,
                None,
                disabled,
                f"未知 type={server_type!r}，且未提供 command",
            )

    timeout = raw.get("timeout", 30_000)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        return McpConfigInspection(name, None, disabled, "timeout 必须是正整数毫秒")

    if server_type == "local":
        command = raw.get("command", [])
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            return McpConfigInspection(name, None, disabled, "local MCP 的 command 必须是非空字符串数组")
        environment = raw.get("environment") or {}
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            return McpConfigInspection(name, None, disabled, "environment 必须是字符串键值对象")
        return McpConfigInspection(
            name,
            McpServerConfig(
                name=name,
                type="local",
                command=list(command),
                environment=dict(environment),
                disabled=disabled,
                timeout=timeout,
            ),
            disabled,
        )

    url = raw.get("url", "")
    if not isinstance(url, str) or not url:
        return McpConfigInspection(name, None, disabled, "remote MCP 缺少 url")
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers.items()
    ):
        return McpConfigInspection(name, None, disabled, "headers 必须是字符串键值对象")
    return McpConfigInspection(
        name,
        McpServerConfig(
            name=name,
            type="remote",
            url=url,
            headers=dict(headers),
            disabled=disabled,
            timeout=timeout,
        ),
        disabled,
    )


def parse_mcp_config(raw: dict[str, Any]) -> list[McpServerConfig]:
    """从 config.json 的 "mcp" 段解析出服务器配置列表。

    Args:
        raw: config.json 中 "mcp" 字段的值，格式为 { server_name: server_config, ... }

    Returns:
        解析成功的 McpServerConfig 列表（跳过 disabled 和解析失败的）
    """
    if not raw or not isinstance(raw, dict):
        return []

    results: list[McpServerConfig] = []
    for name, cfg in raw.items():
        inspection = inspect_mcp_server(name, cfg)
        if inspection.error:
            logger.warning("[mcp-config] 跳过 %s：%s", name, inspection.error)
            continue
        if inspection.disabled:
            logger.info("[mcp-config] 跳过已禁用: %s", name)
            continue
        if inspection.config is not None:
            results.append(inspection.config)

    logger.info(f"[mcp-config] 解析到 {len(results)} 个 MCP 服务器")
    return results
