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

# 默认 system prompt 文件（与 config.py 同级）
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"


def _load_system_prompt() -> str:
    """从 system_prompt.md 读取默认提示词。"""
    try:
        if SYSTEM_PROMPT_PATH.exists():
            return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"[config] 读取 system_prompt.md 失败: {e}")
    return "你是 ftre，一个 AI 编程助手。"


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
class ContextConfig:
    """上下文管理（压缩）配置 —— 对应 config.json 的 agents.defaults.context。

    所有字段都有默认值，缺省即沿用代码内常量；旧配置零改动可用。
    详细设计见文档 docs/context-management.md。
    """
    # 预压缩水位：estimated_tokens / context_window ≥ 此值时后台准备摘要
    precompact_threshold: float = 0.5
    # 启用压缩水位：estimated_tokens / context_window ≥ 此值时启用已准备的摘要
    compact_threshold: float = 0.6
    # 压缩目标比例：target = budget * consolidation_ratio
    consolidation_ratio: float = 0.5
    # 预算安全垫：budget = context_window - max_output - safety_buffer
    safety_buffer: int = 1024
    # 是否开启后台空闲压缩（每轮 done 后异步 LLM 摘要）
    idle_compaction: bool = True
    # 压缩事件是否标记 silent（前端不渲染气泡，对用户无感）
    silent: bool = True


@dataclass
class AgentConfig:
    """Agent 配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    system_prompt: str = ""  # 默认从 system_prompt.md 加载，见 _load_system_prompt()
    # 用户自定义提示词：客户端设置，存于 config.json 的 agents.defaults.user_prompt。
    # 与内置 system_prompt 分离，由 context_govern 插件在每轮构建消息时注入。
    user_prompt: str = ""
    max_iterations: int | None = None
    # 默认工作区。空字符串表示走进程 cwd 兜底。
    # 一个 session 没有 set_workspace 历史时使用这个值；
    # 配置项位于 config.json 的 agents.defaults.workspace。
    workspace: str = ""
    # 标题生成专用 LLM；None 表示沿用主 llm 配置。
    # 配置项：agents.defaults.title_generation = {"provider": "...", "model": "..."}
    # 设计动机：标题生成是高频小请求，独立挂到便宜/快的模型上，避免占用主对话的高级模型配额。
    title_llm: LLMConfig | None = None
    # 上下文压缩专用 LLM；None 表示沿用主 llm 配置。
    # 配置项：agents.defaults.compact_generation = {"provider": "...", "model": "..."}
    # 设计动机：压缩是后台高频长上下文调用，可用便宜/大窗口模型降低成本。
    compact_llm: LLMConfig | None = None
    # 上下文管理配置（步骤 6）
    context: ContextConfig = field(default_factory=ContextConfig)


# ─── 配置缓存 ──────────────────────────────────────────────────────
# load_config() 被高频调用（每条消息派发、usage_update、compact 调度都会触发），
# 每次都读文件+打日志会刷屏。用 _last_config 缓存 + mtime 检测变更：
# - 首次加载 / 文件变更 → INFO 日志
# - 重复加载同一文件 → DEBUG 日志（不再刷屏）
_last_config: AgentConfig | None = None
_last_mtime: float = 0.0


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


def load_gateway_address(default_host: str = "127.0.0.1", default_port: int = 48650) -> tuple[str, int]:
    """读取 gateway 监听地址。

    端口/host 的单一事实源是 config.json 的 servers.gateway，缺失时回退到默认值。
    """
    servers = load_config_file().get("servers", {})
    gateway = servers.get("gateway", {}) if isinstance(servers, dict) else {}
    host = gateway.get("host") if isinstance(gateway, dict) else None
    port = gateway.get("port") if isinstance(gateway, dict) else None
    return (
        host if isinstance(host, str) and host else default_host,
        port if isinstance(port, int) else default_port,
    )


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
    """从配置文件加载 AgentConfig（带缓存，文件变更时才重新解析并 INFO 日志）"""
    global _last_config, _last_mtime

    # mtime 检测：文件没变就直接返回缓存
    try:
        current_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if _last_config is not None and current_mtime == _last_mtime:
        logger.debug("[config] 缓存命中，跳过重新解析")
        return _last_config

    # ─── 重新解析 ──────────────────────────────────────────────
    data = load_config_file()
    if not data:
        # 文件不存在/空时返回默认配置（不缓存，下次文件出现会重新加载）
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

    # 上下文压缩模型（可选）。沿用同一份 providers 配置，但允许指向不同 provider/model。
    compact_llm: LLMConfig | None = None
    compact_cfg = defaults.get("compact_generation") or {}
    if isinstance(compact_cfg, dict):
        c_provider = compact_cfg.get("provider", "") or ""
        c_model = compact_cfg.get("model", "") or ""
        if c_provider and c_model:
            built = _build_llm_config(data, c_provider, c_model)
            if built.model:
                compact_llm = built

    workspace = defaults.get("workspace", "") or ""
    if not isinstance(workspace, str):
        workspace = ""

    # 系统提示词：优先从 config.json 读取，否则从 system_prompt.md 加载
    system_prompt = defaults.get("system_prompt", "") or ""
    if not system_prompt:
        system_prompt = _load_system_prompt()

    # 用户自定义提示词（客户端可设置，缺省为空）
    user_prompt = defaults.get("user_prompt", "") or ""
    if not isinstance(user_prompt, str):
        user_prompt = ""

    # 上下文管理配置：agents.defaults.context（缺省即代码内默认值）
    ctx_raw = defaults.get("context") or {}
    if not isinstance(ctx_raw, dict):
        ctx_raw = {}

    def _f(key_camel: str, key_snake: str, default):
        # 兼容 camelCase 与 snake_case，前者优先
        if key_camel in ctx_raw:
            return ctx_raw[key_camel]
        return ctx_raw.get(key_snake, default)

    context_cfg = ContextConfig(
        precompact_threshold=float(_f("precompactThreshold", "precompact_threshold", 0.5)),
        compact_threshold=float(_f("compactThreshold", "compact_threshold", _f("threshold", "threshold", 0.6))),
        consolidation_ratio=float(_f("consolidationRatio", "consolidation_ratio", 0.5)),
        safety_buffer=int(_f("safetyBuffer", "safety_buffer", 1024)),
        idle_compaction=bool(_f("idleCompaction", "idle_compaction", True)),
        silent=bool(_f("silent", "silent", True)),
    )

    # 配置日志统一降为 DEBUG，避免每次重新加载刷屏
    is_first_load = _last_config is None
    config_changed = not is_first_load and current_mtime != _last_mtime
    logger.debug(
        f"[config] model={llm.model}, provider={provider_name}, "
        f"context_window={llm.context_window}, max_output={llm.max_output}, "
        f"workspace={workspace or '(default)'}, "
        f"title_llm={title_llm.model if title_llm else '(fallback to main)'}, "
        f"compact_llm={compact_llm.model if compact_llm else '(fallback to main)'}, "
        f"context: ratio={context_cfg.consolidation_ratio}, "
        f"precompact={context_cfg.precompact_threshold}, "
        f"compact={context_cfg.compact_threshold}, "
        f"idle={context_cfg.idle_compaction}, silent={context_cfg.silent}"
        + (" (重新加载)" if config_changed else "")
    )

    result = AgentConfig(
        llm=llm,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        workspace=workspace,
        title_llm=title_llm,
        compact_llm=compact_llm,
        context=context_cfg,
    )

    # 更新缓存
    _last_config = result
    _last_mtime = current_mtime

    return result


DEFAULT_CONFIG = load_config()