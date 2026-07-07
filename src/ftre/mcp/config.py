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

当前仅实现 local（stdio）类型；remote 为预留。
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
    def from_raw(cls, name: str, raw: dict) -> "McpServerConfig | None":
        """从单个 server 的 raw dict 构造配置。

        Args:
            name: 服务器名称（mcp 字典的 key）
            raw: 服务器配置内容（mcp 字典的 value）

        Returns:
            McpServerConfig 或 None（disabled / 格式错误时）
        """
        if not isinstance(raw, dict):
            logger.warning(f"[mcp-config] 跳过无效配置: {name}（不是 dict）")
            return None

        server_type = raw.get("type", "")
        if server_type not in ("local", "remote"):
            if "command" in raw:
                server_type = "local"
            else:
                logger.warning(f"[mcp-config] 跳过 {name}：未知 type={server_type!r}，且无 command")
                return None

        disabled = raw.get("disabled", False) or raw.get("enabled", True) is False
        if disabled:
            logger.info(f"[mcp-config] 跳过已禁用: {name}")
            return None

        if server_type == "local":
            command = raw.get("command", [])
            if not command or not isinstance(command, list):
                logger.warning(f"[mcp-config] 跳过 {name}：command 缺失或非数组")
                return None
            return cls(
                name=name,
                type="local",
                command=command,
                environment=raw.get("environment") or {},
                timeout=int(raw.get("timeout", 30_000)),
            )
        elif server_type == "remote":
            url = raw.get("url", "")
            if not url:
                logger.warning(f"[mcp-config] 跳过 {name}：remote 缺少 url")
                return None
            return cls(
                name=name,
                type="remote",
                url=url,
                headers=raw.get("headers") or {},
                timeout=int(raw.get("timeout", 30_000)),
            )
        return None


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
        config = McpServerConfig.from_raw(name, cfg)
        if config:
            results.append(config)

    logger.info(f"[mcp-config] 解析到 {len(results)} 个 MCP 服务器")
    return results
