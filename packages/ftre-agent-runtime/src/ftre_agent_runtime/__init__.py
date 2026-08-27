"""ftre-agent-runtime：AgentLoop 的具体执行实现包。

提供 Runtime Provider Plugin（``plugin:apply``，entry point ``agent-runtime``）、
AgentLoop、AgentLoopFactory、TurnExecutor、Core Agent 工厂和进程内完成注册表。

依赖方向（PRD-F33 §3）：Runtime → ftre-agent（契约）→ ftre-agent-core；
Host Service 以构造参数注入，本包源码不 import ``ftre.services.*`` 实现模块。
"""

from .engine import AgentLoop
from .plugin import apply
from .runtime_factory import AgentLoopFactory
from .turn_executor import TurnExecutor

__all__ = ["AgentLoop", "AgentLoopFactory", "TurnExecutor", "apply"]
