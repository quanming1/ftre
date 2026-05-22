"""
应用配置

从 ~/.ftre/config.json 加载 LLM 和插件配置。
"""
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_PATH = Path(os.environ.get("USERPROFILE", Path.home()) if sys.platform == "win32" else Path.home()) / ".ftre" / "config.json"


@dataclass
class LLMConfig:
    """LLM 配置"""
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    api_type: str = "completions"


@dataclass
class AgentConfig:
    """Agent 配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    system_prompt: str = "你是 ftre，一个 AI 编程助手。"
    max_iterations: int | None = None


def load_config_file() -> dict:
    """读取 ~/.ftre/config.json 原始内容"""
    if not CONFIG_PATH.exists():
        logger.warning(f"[config] 不存在: {CONFIG_PATH}")
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[config] 读取失败: {e}")
        return {}


def _build_model_name(model_id: str, protocol: str) -> str:
    """拼接 LiteLLM 模型名。已有 / 前缀的不重复加。"""
    if "/" in model_id:
        return model_id
    prefix = {"openai": "openai", "anthropic": "anthropic", "gemini": "gemini", "azure": "azure", "bedrock": "bedrock"}.get(protocol, "openai")
    return f"{prefix}/{model_id}"


def load_config() -> AgentConfig:
    """从配置文件加载 AgentConfig"""
    data = load_config_file()
    if not data:
        return AgentConfig()

    defaults = data.get("agents", {}).get("defaults", {})
    model_id = defaults.get("model", "")
    provider_name = defaults.get("provider", "")

    provider = data.get("providers", {}).get(provider_name, {})
    protocol = provider.get("api_protocol", "openai")

    llm = LLMConfig(
        model=_build_model_name(model_id, protocol) if model_id else "",
        api_key=provider.get("api_key", ""),
        api_base=provider.get("api_base", ""),
    )

    logger.warning(f"[config] model={llm.model}, provider={provider_name}")
    return AgentConfig(llm=llm)


DEFAULT_CONFIG = load_config()
