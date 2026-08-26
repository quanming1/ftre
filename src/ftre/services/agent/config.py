"""
应用配置

从 ~/.ftre/config.json 加载 LLM 和插件配置。
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ftre.services.config.loader import load_config_file
from ftre.services.config.paths import AGENTS_DIR, CONFIG_PATH

logger = logging.getLogger(__name__)

# 默认 system prompt 文件由应用包统一提供。
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "system_prompt.md"


def _load_system_prompt() -> str:
    """从 system_prompt.md 读取默认提示词，用 XML 标签包裹。"""
    try:
        if SYSTEM_PROMPT_PATH.exists():
            content = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
            return f'<SYSTEM_PROMPT desc="系统提示词基座，定义运行时行为规范" path="{SYSTEM_PROMPT_PATH}">\n{content}\n</SYSTEM_PROMPT>'
    except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
        logger.warning(f"[config] 读取 system_prompt.md 失败: {e}")
    return f'<SYSTEM_PROMPT desc="系统提示词基座，定义运行时行为规范" path="{SYSTEM_PROMPT_PATH}">\n- 你是一个 AI 助手。\n</SYSTEM_PROMPT>'


def _read_default_agent_reasoning_effort() -> str | None:
    """读取 default Agent 显式配置的 reasoning_effort，缺失时返回 None。"""
    cfg_path = AGENTS_DIR / "default" / "agent.config.json"
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        llm = cfg.get("llm", {})
        if not isinstance(llm, dict) or "reasoning_effort" not in llm:
            return None
        effort = llm["reasoning_effort"]
        return effort if isinstance(effort, str) else ""
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[config] 读取 default agent 配置失败: {e}")
        return None


def _read_default_agent_llm() -> tuple[str, str, str]:
    """读取 default agent 的 llm provider/model 和 workspace。

    全局兜底配置的单一事实源：model/provider/workspace 不再放在 config.json 的
    agents.defaults 中，而是由 ~/.ftre/agents/default/agent.config.json 持有。
    前端切换 default agent 模型时写此文件，load_config() 即可读到最新值。

    Returns: (provider, model, workspace)
    """
    cfg_path = AGENTS_DIR / "default" / "agent.config.json"
    if not cfg_path.exists():
        return "", "", ""
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        llm = cfg.get("llm", {})
        if not isinstance(llm, dict):
            llm = {}
        workspace = cfg.get("workspace", "")
        if not isinstance(workspace, str):
            workspace = ""
        return llm.get("provider", ""), llm.get("model", ""), workspace
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[config] 读取 default agent 配置失败: {e}")
        return "", "", ""


@dataclass
class LLMConfig:
    """
    LLM 配置 —— 字段与 ~/.ftre/config.json 保持一致：

    - 来自 providers[provider]：api_key / api_base / api_type
    - 来自 providers[provider].models[] 中匹配 default model 的条目：
      name / id / context_window / max_output / vision

    `model` 是派生字段，当前由 `_build_model_name()` 直接返回 `model_id`（不做前缀拼接），
    供 ReActAgent 直接使用。原始 id 保留在 `id` 里，避免上层重复解析。
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
    """Agent 配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    system_prompt: str = ""  # 默认从 system_prompt.md 加载，见 _load_system_prompt()
    max_iterations: int | None = None
    # 默认工作区。空字符串表示走进程 cwd 兜底。
    # 创建新 session 时作为预填值。
    # 配置项：config.json 的 default_workspace（顶层）。
    workspace: str = ""
    # 标题生成专用 LLM；None 表示沿用主 llm 配置。
    # 配置项：agents.title_generation = {"provider": "...", "model": "..."}
    # 设计动机：标题生成是高频小请求，独立挂到便宜/快的模型上，避免占用主对话的高级模型配额。
    title_llm: LLMConfig | None = None


# ─── 配置缓存 ──────────────────────────────────────────────────────
# load_config() 被高频调用（每条消息派发、usage 聚合、compact 调度都会触发），
# 每次都读文件+打日志会刷屏。用 _last_config 缓存 + mtime 检测变更：
# - 首次加载 / 文件变更 → INFO 日志
# - 重复加载同一文件 → DEBUG 日志（不再刷屏）
# 缓存签名同时跟踪 config.json 和 default agent config 的 mtime，
# 因为 model/provider/workspace 现在从 default agent 读取。
_last_config: AgentConfig | None = None
_last_sig: str = ""


def _build_model_name(model_id: str, protocol: str) -> str:
    return model_id


def _find_model_entry(provider: dict, model_id: str) -> dict:
    """从 provider.models 里找到 id==model_id 的条目；找不到返回空 dict"""
    if not model_id:
        return {}
    for m in provider.get("models", []) or []:
        if isinstance(m, dict) and m.get("id") == model_id:
            return m
    return {}


def build_llm_config(data: dict, provider_name: str, model_id: str) -> LLMConfig:
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

    Args:
        effort: agent 显式配置的 effort（可能为 "" 表示未设置/清空）
        llm: 目标模型的 LLMConfig（含模型级 reasoning_effort / reasoning_effort_values）

    Returns:
        落到目标模型的 effort；不支持时返回 ""。
    """
    if not isinstance(effort, str) or not effort:
        return ""
    if not llm.reasoning_effort and not llm.reasoning_effort_values:
        return ""
    return effort


def load_config() -> AgentConfig:
    """从配置文件加载 AgentConfig（带缓存，文件变更时才重新解析并 INFO 日志）。

    配置来源：
    - model / provider / workspace → ~/.ftre/agents/default/agent.config.json
    - title_generation → config.json 的 agents 顶层
    - Inbox 容量由 ftre-inbox 包自行解析
    - system_prompt → system_prompt.md 文件
    """
    global _last_config, _last_sig

    # mtime 检测：config.json + default agent config
    try:
        current_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    da_cfg_path = AGENTS_DIR / "default" / "agent.config.json"
    try:
        da_mtime = da_cfg_path.stat().st_mtime if da_cfg_path.exists() else 0.0
    except OSError:
        da_mtime = 0.0

    current_sig = f"{current_mtime}|{da_mtime}"

    if _last_config is not None and current_sig == _last_sig:
        logger.debug("[config] 缓存命中，跳过重新解析")
        return _last_config

    # ─── 重新解析 ──────────────────────────────────────────────
    data = load_config_file()
    if not data:
        # 文件不存在/空时返回默认配置（不缓存，下次文件出现会重新加载）
        return AgentConfig()

    agents_cfg = data.get("agents", {})
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}

    # ─── model / provider：从 default agent 读取 ───
    da_provider, da_model, _ = _read_default_agent_llm()
    provider_name = da_provider
    model_id = da_model

    # ─── workspace：从 config.json 的 default_workspace 读取 ───
    workspace = data.get("default_workspace", "") or ""
    if not isinstance(workspace, str):
        workspace = ""

    llm = build_llm_config(data, provider_name, model_id)
    default_effort = _read_default_agent_reasoning_effort()
    if default_effort is not None:
        llm.reasoning_effort = sanitize_agent_effort(default_effort, llm)

    # 标题生成模型（可选）。沿用同一份 providers 配置，但允许指向不同 provider/model。
    title_llm: LLMConfig | None = None
    title_cfg = agents_cfg.get("title_generation") or {}
    if isinstance(title_cfg, dict):
        t_provider = title_cfg.get("provider", "") or ""
        t_model = title_cfg.get("model", "") or ""
        if t_provider and t_model:
            built = build_llm_config(data, t_provider, t_model)
            # 没找到 model 条目时 built.model 为空 —— 此时不启用，回到主 llm 兜底
            if built.model:
                title_llm = built

    # 系统提示词：从 system_prompt.md 文件加载
    system_prompt = _load_system_prompt()

    # 配置日志统一降为 DEBUG，避免每次重新加载刷屏
    is_first_load = _last_config is None
    config_changed = not is_first_load and current_sig != _last_sig
    logger.debug(
        f"[config] model={llm.model}, provider={provider_name}, "
        f"context_window={llm.context_window}, max_output={llm.max_output}, "
        f"workspace={workspace or '(default)'}, "
        f"title_llm={title_llm.model if title_llm else '(fallback to main)'}, "
        + (" (重新加载)" if config_changed else "")
    )

    result = AgentConfig(
        llm=llm,
        system_prompt=system_prompt,
        workspace=workspace,
        title_llm=title_llm,
    )

    # 更新缓存
    _last_config = result
    _last_sig = current_sig

    return result
