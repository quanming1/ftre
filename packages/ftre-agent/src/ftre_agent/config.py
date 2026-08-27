"""Agent 配置的跨包稳定契约。

``LLMConfig``/``AgentConfig`` 被 ftre-agent 的 Hook payload、ftre-agent-runtime
的 Turn 执行、ftre-compaction 的水位判断和 Host 的配置加载共同消费，因此按
PRD-F33 §5.4 作为"最小、稳定、可复用的契约"提取到本包。

这里只包含纯数据结构与无副作用的解析函数；读取 ``~/.ftre`` 磁盘配置、缓存和
system_prompt.md 的逻辑仍由 Host 的 ``ftre.services.agent_profile.config`` 唯一持有。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置 —— 字段与 ~/.ftre/config.json 保持一致。

    - 来自 providers[provider]：api_key / api_base / api_type
    - 来自 providers[provider].models[] 中匹配 default model 的条目：
      name / id / context_window / max_output / vision

    ``model`` 是派生字段，当前由 ``_build_model_name()`` 直接返回 ``model_id``
    （不做前缀拼接），供 ReActAgent 直接使用。原始 id 保留在 ``id`` 里，
    避免上层重复解析。
    """

    # provider 层：必须保留逻辑 Provider 名称，供 LLM 路由、Hook 和日志关联。
    provider: str = ""
    api_key: str = ""
    api_base: str = ""
    api_type: str = "completions"
    # model 条目层（与 config.json models[] 同名）
    name: str = ""
    id: str = ""
    context_window: int | None = None
    max_output: int | None = None
    vision: bool = False
    reasoning_effort: str = ""
    # 模型声明支持的推理强度可选值（config.json models[] 的 reasoning_effort_values）。
    # 空 tuple = 该模型未声明任何推理强度配置（不支持此参数），
    # agent 显式配置的 effort 应被忽略（见 sanitize_agent_effort）。
    reasoning_effort_values: tuple[str, ...] = ()
    # 派生：LiteLLM 模型名（含 provider 前缀）
    model: str = ""


@dataclass
class AgentConfig:
    """Agent 运行配置快照。

    一个 Turn 在进入执行前冻结本快照；Hook 门控与真实执行使用同一份，
    避免"Hook 判断用旧配置、执行用新配置"的窗口期。
    """

    llm: LLMConfig = field(default_factory=LLMConfig)
    system_prompt: str = ""  # 默认从 system_prompt.md 加载，由 Host config 决定
    max_iterations: int | None = None
    # 默认工作区。空字符串表示走进程 cwd 兜底。
    # 创建新 session 时作为预填值。
    # 配置项：config.json 的 default_workspace（顶层）。
    workspace: str = ""
    # 标题生成专用 LLM；None 表示沿用主 llm 配置。
    # 配置项：agents.title_generation = {"provider": "...", "model": "..."}
    # 设计动机：标题生成是高频小请求，独立挂到便宜/快的模型上，避免占用主对话的高级模型配额。
    title_llm: LLMConfig | None = None


def _build_model_name(model_id: str, protocol: str) -> str:
    return model_id


def _find_model_entry(provider: dict, model_id: str) -> dict:
    """从 provider.models 里找到 id==model_id 的条目；找不到返回空 dict。"""
    if not model_id:
        return {}
    for m in provider.get("models", []) or []:
        if isinstance(m, dict) and m.get("id") == model_id:
            return m
    return {}


def build_llm_config(data: dict, provider_name: str, model_id: str) -> LLMConfig:
    """根据顶层 config dict + provider + model id，构造一个 LLMConfig。

    传入的 model_id 在 provider.models 里找不到就回到空 LLMConfig（model=""
    表示未配置，调用方据此决定是否启用相关功能）。
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
    raw_values = model_entry.get("reasoning_effort_values")
    # api_type 三级回退（A1 FR6）：model 条目 > provider 级 > 默认 completions。
    # 同一 provider 内可按模型混合协议（如 OpenCode Go：Muse/Luna 走 responses、
    # 其余走 chat/completions）。
    raw_api_type = model_entry.get("api_type") or provider.get("api_type") or "completions"
    return LLMConfig(
        provider=provider_name,
        api_key=provider.get("api_key", ""),
        api_base=provider.get("api_base", ""),
        api_type=raw_api_type if isinstance(raw_api_type, str) else "completions",
        name=model_entry.get("name", ""),
        id=model_id,
        context_window=cw if isinstance(cw, int) else None,
        max_output=mo if isinstance(mo, int) else None,
        vision=bool(model_entry.get("vision", False)),
        reasoning_effort=model_entry.get("reasoning_effort", "") if isinstance(model_entry.get("reasoning_effort", ""), str) else "",
        reasoning_effort_values=(
            tuple(v for v in raw_values if isinstance(v, str))
            if isinstance(raw_values, list)
            else ()
        ),
        model=_build_model_name(model_id, protocol),
    )


def sanitize_agent_effort(effort: str, llm: LLMConfig) -> str:
    """把 agent 显式配置的 reasoning_effort 落到目标模型上，若该模型不支持则清空。

    判断依据：模型条目是否声明了推理强度配置（reasoning_effort 默认值或
    reasoning_effort_values 可选值）。两者都没有 = 模型不支持此参数，
    任何显式 effort（如上一个支持推理模型残留的 "max"）都会被上游拒绝
    （如"该模型始终思考，不支持关闭思考"），必须丢弃，避免请求 400。
    """
    if not isinstance(effort, str) or not effort:
        return ""
    if not llm.reasoning_effort and not llm.reasoning_effort_values:
        return ""
    return effort


__all__ = [
    "AgentConfig",
    "LLMConfig",
    "build_llm_config",
    "sanitize_agent_effort",
]
