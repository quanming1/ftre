"""Host 侧 Agent 域：配置加载与 Agent Profile Service。

Agent 的稳定契约（AgentService/InboundMessage/AgentRunResult/Agent Hook/
AgentRegistry）自 F33 起由 ``ftre-agent`` 契约包提供，具体执行 Runtime 由
``ftre-agent-runtime`` 的 Provider Plugin 装配；本包只保留磁盘配置加载
（``config.load_config``）与 Agent Profile 能力（``profile``）。
"""

__all__ = ["config", "profile"]
