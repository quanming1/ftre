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
    """
    LLM 配置 —— 字段与 ~/.ftre/config.json 保持一致：

    - 来自 providers[provider]：api_key / api_base / api_type
    - 来自 providers[provider].models[] 中匹配 default model 的条目：
      name / id / context_window / max_output / vision

    `model` 是派生字段：把 id 加上 LiteLLM 需要的 provider 前缀（如 'openai/'），
    供 ReActAgent 直接使用。原始 id 保留在 `id` 里，避免上层重复解析。
    """
    # provider 层
    api_key: str = ""
    api_base: str = ""
    api_type: str = "completions"
    # model 条目层（与 config.json models[] 同名）
    name: str = ""
    id: str = ""
    context_window: int | None = None
    max_output: int | None = None
    vision: bool = False
    # 派生：LiteLLM 模型名（含 provider 前缀）
    model: str = ""


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


def _find_model_entry(provider: dict, model_id: str) -> dict:
    """从 provider.models 里找到 id==model_id 的条目；找不到返回空 dict"""
    if not model_id:
        return {}
    for m in provider.get("models", []) or []:
        if isinstance(m, dict) and m.get("id") == model_id:
            return m
    return {}


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
    model_entry = _find_model_entry(provider, model_id)

    cw = model_entry.get("context_window")
    mo = model_entry.get("max_output")

    llm = LLMConfig(
        # provider 层
        api_key=provider.get("api_key", ""),
        api_base=provider.get("api_base", ""),
        # model 条目层（与 config.json 字段同名）
        name=model_entry.get("name", ""),
        id=model_id,
        context_window=cw if isinstance(cw, int) else None,
        max_output=mo if isinstance(mo, int) else None,
        vision=bool(model_entry.get("vision", False)),
        # 派生
        model=_build_model_name(model_id, protocol) if model_id else "",
    )

    logger.warning(
        f"[config] model={llm.model}, provider={provider_name}, "
        f"context_window={llm.context_window}, max_output={llm.max_output}"
    )
    return AgentConfig(llm=llm)


DEFAULT_CONFIG = load_config()
