"""Raw gateway configuration loading owned by the Config layer."""

from __future__ import annotations

import json
import logging

from .paths import CONFIG_PATH

logger = logging.getLogger(__name__)


def load_config_file() -> dict:
    """Read the raw ``~/.ftre/config.json`` object without creating a second cache."""
    if not CONFIG_PATH.exists():
        logger.warning("[config] 不存在: %s", CONFIG_PATH)
        return {}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("[config] 读取失败: %s", exc)
        return {}
    if not isinstance(value, dict):
        logger.error("[config] 根值必须是对象")
        return {}
    return value


def load_gateway_address(
    default_host: str = "127.0.0.1", default_port: int = 48650
) -> tuple[str, int]:
    """Read the Gateway listen address from the raw Config layer."""
    servers = load_config_file().get("servers", {})
    gateway = servers.get("gateway", {}) if isinstance(servers, dict) else {}
    host = gateway.get("host") if isinstance(gateway, dict) else None
    port = gateway.get("port") if isinstance(gateway, dict) else None
    return (
        host if isinstance(host, str) and host else default_host,
        port if isinstance(port, int) else default_port,
    )


__all__ = ["load_config_file", "load_gateway_address"]
