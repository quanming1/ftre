"""
Host 侧 Agent 配置加载。

从 ~/.ftre/config.json 和 ~/.ftre/agents/default/agent.config.json 加载 LLM
和插件配置，并维护 mtime 缓存。``LLMConfig``/``AgentConfig`` 等纯数据契约
自 F33 起由契约包 ``ftre_agent`` 唯一提供（Hook payload、Runtime、压缩包
共同消费）；本模块只保留磁盘读取与缓存逻辑。
"""
import json
import logging
from pathlib import Path

from ftre_agent import (
    AgentConfig,
    LLMConfig,
    build_llm_config,
    sanitize_agent_effort,
)

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


# ─── 配置缓存 ──────────────────────────────────────────────────────
# load_config() 被高频调用（每条消息派发、usage 聚合、compact 调度都会触发），
# 每次都读文件+打日志会刷屏。用 _last_config 缓存 + mtime 检测变更：
# - 首次加载 / 文件变更 → INFO 日志
# - 重复加载同一文件 → DEBUG 日志（不再刷屏）
# 缓存签名同时跟踪 config.json 和 default agent config 的 mtime，
# 因为 model/provider/workspace 现在从 default agent 读取。
_last_config: AgentConfig | None = None
_last_sig: str = ""


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
