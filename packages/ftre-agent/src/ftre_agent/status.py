"""Agent 状态的稳定字面量契约。

状态集合是 AgentService 对外承诺的一部分：调用方（Inbox、HTTP、Channel）
依赖这些字符串判断会话可见行为，因此在这里冻结，不随 Runtime 实现变化。
"""

from typing import Literal

# idle：没有 active Turn，也没有 Runtime 维护屏障；
# running：一个 active Turn 正在执行；
# processing：历史保留值，与 running 同义（旧客户端兼容语义，不新增使用方）；
# compacting：Turn 已结束，但 after-run 维护（如压缩）仍在进行。
AgentStatus = Literal["idle", "running", "processing", "compacting"]

__all__ = ["AgentStatus"]
