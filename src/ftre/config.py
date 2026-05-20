"""
应用配置
"""
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """LLM 配置"""
    model: str = "openai/DeepSeek-V3.2"
    api_key: str = ""
    api_base: str = ""
    api_type: str = "completions"


@dataclass
class AgentConfig:
    """Agent 配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    system_prompt: str = ""
    max_iterations: int | None = None  # None = 无限循环


# 全局默认配置
DEFAULT_CONFIG = AgentConfig(
    llm=LLMConfig(
        model="openai/DeepSeek-V3.2",
        api_key="sk-HIYFHsm6Oyx1MotZXpxtXOMfDGj6azzPKw3GPQX4RxASrAZH",
        api_base="https://llm-gateway.mlamp.cn/v1",
    ),
    system_prompt="你是 ftre，一个 AI 编程助手。",
)
