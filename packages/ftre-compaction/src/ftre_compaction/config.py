"""ftre-compaction 的配置 Owner。

压缩是可选能力，因此它的阈值、摘要模型和预算安全垫不能继续放在
ftre 核心的 ``AgentConfig`` 里。这个模块只负责把原始 ``config.json``
中的压缩配置解析成一个不可变快照；真正的 Agent 模型配置仍由本轮
``AgentConfig.llm`` 提供。

配置来源约定：

* agents.context：压缩阈值、预算安全余量和 token 分块参数；
* ``agents.compact_generation``：可选的摘要专用 provider/model；
* 缺省值由本包提供，不依赖 ftre 核心是否曾经实现过压缩配置。

解析结果是一次 Hook/Command 调用的快照。这样配置热更新不会改变已经
开始的压缩任务，下一次调用才会读取新的设置。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ftre_agent import LLMConfig, build_llm_config

DEFAULT_PRECOMPACT_THRESHOLD = 0.7
DEFAULT_COMPACT_THRESHOLD = 0.8
DEFAULT_SAFETY_BUFFER = 1024
DEFAULT_CHUNK_TOKENS = 200_000
DEFAULT_CHUNK_PARALLELISM = 4
DEFAULT_CHUNK_TIMEOUT_SECONDS = 120.0
DEFAULT_CHUNK_RETRY_ATTEMPTS = 1
MIN_CHUNK_TOKENS = 16_000
MAX_CHUNK_TOKENS = 1_000_000
MAX_CHUNK_PARALLELISM = 8
MAX_CHUNK_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """一次压缩操作使用的完整配置快照。

    ``llm`` 只表示“压缩专用模型覆盖”；为空时由 Service 回退到本轮
    Agent 的主模型。上下文窗口、最大输出等预算输入永远来自本轮 Agent
    的主 ``AgentConfig.llm``，避免用全局默认模型误判多 Agent 场景。
    """

    precompact_threshold: float = DEFAULT_PRECOMPACT_THRESHOLD
    compact_threshold: float = DEFAULT_COMPACT_THRESHOLD
    safety_buffer: int = DEFAULT_SAFETY_BUFFER
    llm: LLMConfig | None = None
    # 分块参数属于 Package；chunk LLM 只是在同一个 Service Task 内运行，
    # 不会变成额外的 Plugin/Service。
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS
    chunk_parallelism: int = DEFAULT_CHUNK_PARALLELISM
    chunk_timeout_seconds: float = DEFAULT_CHUNK_TIMEOUT_SECONDS
    chunk_retry_attempts: int = DEFAULT_CHUNK_RETRY_ATTEMPTS


def parse_compaction_config(
    raw: Mapping[str, Any] | None,
    *,
    defaults: CompactionConfig | None = None,
) -> CompactionConfig:
    """从原始配置解析压缩快照，并对坏值进行安全降级。

    解析器故意不修改传入字典，也不保存全局缓存。压缩包可以把它接到
    ``ConfigService.snapshot()``，从而在每次 Hook 边界读取最新配置。
    摘要模型缺失时不在这里复制 Agent 模型；由 Service 在真正调用时回退。
    """

    base = defaults or CompactionConfig()
    root = raw if isinstance(raw, Mapping) else {}
    agents = root.get("agents", {})
    if not isinstance(agents, Mapping):
        agents = {}
    context = agents.get("context", {})
    if not isinstance(context, Mapping):
        context = {}

    compact_threshold = _number(
        context,
        "compactThreshold",
        "compact_threshold",
        base.compact_threshold,
    )
    # 旧配置中的 threshold 是 compactThreshold 的历史简写；只在两个
    # 正式键都缺失时使用，避免悄悄覆盖用户显式设置。
    if "compactThreshold" not in context and "compact_threshold" not in context:
        compact_threshold = _number(
            context, "threshold", "threshold", compact_threshold
        )

    compact_llm = _parse_compact_llm(root, agents)
    return CompactionConfig(
        precompact_threshold=_number(
            context,
            "precompactThreshold",
            "precompact_threshold",
            base.precompact_threshold,
        ),
        compact_threshold=compact_threshold,
        safety_buffer=_integer(
            context,
            "safetyBuffer",
            "safety_buffer",
            base.safety_buffer,
        ),
        # 没有专用摘要模型时保留默认快照中的覆盖值；若默认值也为空，
        # Service 才会回退到本轮 AgentConfig.llm。
        llm=compact_llm or base.llm,
        chunk_tokens=min(
            MAX_CHUNK_TOKENS,
            max(
                MIN_CHUNK_TOKENS,
                _integer(context, "chunkTokens", "chunk_tokens", base.chunk_tokens),
            ),
        ),
        chunk_parallelism=min(
            MAX_CHUNK_PARALLELISM,
            max(
                1,
                _integer(
                    context,
                    "chunkParallelism",
                    "chunk_parallelism",
                    base.chunk_parallelism,
                ),
            ),
        ),
        chunk_timeout_seconds=min(
            MAX_CHUNK_TIMEOUT_SECONDS,
            max(
                5.0,
                _number(
                    context,
                    "chunkTimeoutSeconds",
                    "chunk_timeout_seconds",
                    base.chunk_timeout_seconds,
                ),
            ),
        ),
        chunk_retry_attempts=min(
            2,
            max(
                0,
                _integer(
                    context,
                    "chunkRetryAttempts",
                    "chunk_retry_attempts",
                    base.chunk_retry_attempts,
                ),
            ),
        ),
    )


def _parse_compact_llm(
    root: Mapping[str, Any], agents: Mapping[str, Any]
) -> LLMConfig | None:
    """解析摘要专用模型；配置缺失或模型不存在时返回 ``None``。"""

    raw = agents.get("compact_generation", {})
    if not isinstance(raw, Mapping):
        return None
    provider = raw.get("provider", "")
    model = raw.get("model", "")
    if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
        return None
    built = build_llm_config(dict(root), provider, model)
    return built if built.model else None


def _number(
    mapping: Mapping[str, Any], first: str, second: str, default: float
) -> float:
    """读取 camelCase/snake_case 数字；非法值回退默认值。"""

    value = mapping.get(first, mapping.get(second, default))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(
    mapping: Mapping[str, Any], first: str, second: str, default: int
) -> int:
    """读取整数型预算配置；非法值回退默认值并禁止负安全余量。"""

    value = mapping.get(first, mapping.get(second, default))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


__all__ = [
    "DEFAULT_CHUNK_PARALLELISM",
    "DEFAULT_CHUNK_RETRY_ATTEMPTS",
    "DEFAULT_CHUNK_TIMEOUT_SECONDS",
    "DEFAULT_CHUNK_TOKENS",
    "DEFAULT_COMPACT_THRESHOLD",
    "DEFAULT_PRECOMPACT_THRESHOLD",
    "DEFAULT_SAFETY_BUFFER",
    "MAX_CHUNK_PARALLELISM",
    "MAX_CHUNK_TIMEOUT_SECONDS",
    "MAX_CHUNK_TOKENS",
    "MIN_CHUNK_TOKENS",
    "CompactionConfig",
    "parse_compaction_config",
]
