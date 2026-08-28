"""Agent 状态的稳定字面量契约。

状态集合是 AgentService 对外承诺的一部分：调用方（Inbox、HTTP、Channel）
依赖这些字符串判断会话可见行为，因此在这里冻结，不随 Runtime 实现变化。
"""

from typing import Literal

# created：Agent 身份已登记但还没有可运行 Handle；
# idle：没有 active Turn，也没有 Runtime 维护屏障；
# running/stopping：Run 正在执行或等待取消收尾；
# cancelled/failed：最近一次 Run 的终态；
# compacting：Turn 已结束，但 after-run 维护（如压缩）仍在进行；
# processing 是已有客户端的历史别名，保留读取兼容，不再由新代码写入；
# disposed：Agent 身份已释放，只用于事件快照。
AgentStatus = Literal[
    "created",
    "idle",
    "running",
    "stopping",
    "cancelled",
    "failed",
    "compacting",
    "processing",
    "disposed",
]

__all__ = ["AgentStatus"]
