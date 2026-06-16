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
        if not isinstance(cfg, dict):
            logger.warning(f"[mcp-config] 跳过无效配置: {name}（不是 dict）")
            continue

        server_type = cfg.get("type", "")
        if server_type not in ("local", "remote"):
            # 宽松兼容：有 command 但没 type 时按 local 处理
            if "command" in cfg:
                server_type = "local"
            else:
                logger.warning(f"[mcp-config] 跳过 {name}：未知 type={server_type!r}，且无 command")
                continue

        disabled = cfg.get("disabled", False) or cfg.get("enabled", True) is False
        if disabled:
            logger.info(f"[mcp-config] 跳过已禁用: {name}")
            continue

        if server_type == "local":
            command = cfg.get("command", [])
            if not command or not isinstance(command, list):
                logger.warning(f"[mcp-config] 跳过 {name}：command 缺失或非数组")
                continue
            results.append(McpServerConfig(
                name=name,
                type="local",
                command=command,
                environment=cfg.get("environment") or {},
                timeout=int(cfg.get("timeout", 30_000)),
            ))
        elif server_type == "remote":
            url = cfg.get("url", "")
            if not url:
                logger.warning(f"[mcp-config] 跳过 {name}：remote 缺少 url")
                continue
            results.append(McpServerConfig(
                name=name,
                type="remote",
                url=url,
                headers=cfg.get("headers") or {},
                timeout=int(cfg.get("timeout", 30_000)),
            ))

    logger.info(f"[mcp-config] 解析到 {len(results)} 个 MCP 服务器")
    return results
