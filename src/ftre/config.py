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
    # 默认工作区。空字符串表示走进程 cwd 兜底。
    # 一个 session 没有 set_workspace 历史时使用这个值；
    # 配置项位于 config.json 的 agents.defaults.workspace。
    workspace: str = ""
    # 标题生成专用 LLM；None 表示沿用主 llm 配置。
    # 配置项：agents.defaults.title_generation = {"provider": "...", "model": "..."}
    # 设计动机：标题生成是高频小请求，独立挂到便宜/快的模型上，避免占用主对话的高级模型配额。
    title_llm: LLMConfig | None = None


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
    """
    拼接 LiteLLM 模型名。

    LiteLLM 的模型名格式是 `<provider>/<model>`，provider 必须是它已知的列表
    （openai / anthropic / azure / gemini / bedrock / groq ...）。

    判定"是否已加前缀"必须用 provider 白名单，不能用 `"/" in model_id`：
    很多网关侧的模型 id 本身就含 `/`（如 `mlamp/kimi-k2.6`、
    `tencent/deepseek-v4-pro`），那个 `/` 是模型名的一部分，不是 provider 前缀。
    把这种 id 直接当成"已带前缀"丢给 LiteLLM 会报
    `LLM Provider NOT provided`。
    """
    prefix = {
        "openai": "openai",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "azure": "azure",
        "bedrock": "bedrock",
        "minimax": "minimax",
    }.get(protocol, "openai")

    # 已知的 LiteLLM provider 前缀；命中即说明 id 已带前缀，无需重复
    KNOWN_LITELLM_PREFIXES = (
        "openai/", "anthropic/", "azure/", "gemini/", "bedrock/",
        "minimax/", "groq/", "vertex_ai/", "ollama/", "huggingface/", "cohere/",
        "mistral/", "deepseek/", "together_ai/", "replicate/",
    )
    for p in KNOWN_LITELLM_PREFIXES:
        if model_id.startswith(p):
            return model_id

    return f"{prefix}/{model_id}"


def _find_model_entry(provider: dict, model_id: str) -> dict:
    """从 provider.models 里找到 id==model_id 的条目；找不到返回空 dict"""
    if not model_id:
        return {}
    for m in provider.get("models", []) or []:
        if isinstance(m, dict) and m.get("id") == model_id:
            return m
    return {}


def _build_llm_config(data: dict, provider_name: str, model_id: str) -> LLMConfig:
    """
    根据顶层 config dict + provider + model id，构造一个 LLMConfig。

    传入的 model_id 在 provider.models 里找不到就回到空 LLMConfig（model="" 表示未配置，
    调用方据此决定是否启用相关功能）。
    """
    if not provider_name or not model_id:
        return LLMConfig()
    provider = data.get("providers", {}).get(provider_name, {})
    if not provider:
        return LLMConfig()
    protocol = provider.get("api_protocol", "openai")
    model_entry = _find_model_entry(provider, model_id)

    cw = model_entry.get("context_window")
    mo = model_entry.get("max_output")
    return LLMConfig(
        api_key=provider.get("api_key", ""),
        api_base=provider.get("api_base", ""),
        name=model_entry.get("name", ""),
        id=model_id,
        context_window=cw if isinstance(cw, int) else None,
        max_output=mo if isinstance(mo, int) else None,
        vision=bool(model_entry.get("vision", False)),
        model=_build_model_name(model_id, protocol),
    )


def load_config() -> AgentConfig:
    """从配置文件加载 AgentConfig"""
    data = load_config_file()
    if not data:
        return AgentConfig()

    defaults = data.get("agents", {}).get("defaults", {})
    model_id = defaults.get("model", "")
    provider_name = defaults.get("provider", "")
    llm = _build_llm_config(data, provider_name, model_id)

    # 标题生成模型（可选）。沿用同一份 providers 配置，但允许指向不同 provider/model。
    title_llm: LLMConfig | None = None
    title_cfg = defaults.get("title_generation") or {}
    if isinstance(title_cfg, dict):
        t_provider = title_cfg.get("provider", "") or ""
        t_model = title_cfg.get("model", "") or ""
        if t_provider and t_model:
            built = _build_llm_config(data, t_provider, t_model)
            # 没找到 model 条目时 built.model 为空 —— 此时不启用，回到主 llm 兜底
            if built.model:
                title_llm = built

    workspace = defaults.get("workspace", "") or ""
    if not isinstance(workspace, str):
        workspace = ""

    logger.warning(
        f"[config] model={llm.model}, provider={provider_name}, "
        f"context_window={llm.context_window}, max_output={llm.max_output}, "
        f"workspace={workspace or '(default)'}, "
        f"title_llm={title_llm.model if title_llm else '(fallback to main)'}"
    )
    return AgentConfig(llm=llm, workspace=workspace, title_llm=title_llm)


DEFAULT_CONFIG = load_config()
