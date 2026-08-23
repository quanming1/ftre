# ftre 语义 Hook 契约

`kernel/hooks` 是 ftre 对官方 `cordis-py` Events 的窄适配，不拥有业务状态，也不
复制 Fiber 生命周期。Plugin 通过自己的 `Context` 注册监听器，卸载时由 Cordis
Effect 自动注销。

```python
from ftre.kernel.hooks import HookMode, HookRuntime, HookSpec

spec = HookSpec(
    "agent/before-turn",
    "agent",
    HookMode.WATERFALL,
    payload_type=BeforeTurnPayload,
    result_type=AllowTurn | RejectTurn,
    default=continue_step,
)
runtime = HookRuntime(ctx)
receipt = runtime.register(
    spec,
    before_step,
    owner="context-govern",
    prepend=True,
)
```

约束：

- `HookSpec.name` 必须是稳定的 `domain/name`，并且 domain 与前缀一致。
- `payload_type` 和控制型 Hook 的 `result_type` 必须显式声明；监听器不原地修改
  其他 Plugin 可见输入。
- `waterfall` 监听器接收 `next_()`；同一监听器重复调用会被拒绝并记录诊断。
- `once` 和 `prepend` 只表达明确语义，不允许任意数字优先级。
- Agent scope 使用 `HookScopeCarrier` + Cordis isolate Context 的对象身份；父 scope
  可命中后代，兄弟和同 id 重建的 Agent 不会串扰。诊断中的 scope 文本只是标签，不能
  替代运行时身份。
- 诊断只记录 Hook、owner、scope、顺序、异常类型和活跃调用数，不记录 payload。

消息队列、SessionEvent 和公开 Service 不由 Hook 替代：Hook 只承担进程内扩展控制。

## Owner 规则

Kernel 不维护业务 Hook 名称总表。Agent、Session、Tool、System Prompt、LLM、Inbox 和
Compaction 必须在自己的 Service/Package 中定义 HookSpec，并通过 `HookRuntime` 注册。
这样一个可选 Plugin 可以卸载，而 Kernel 不需要知道它是否存在。

业务 Hook 的公开文档由对应 Owner 维护；本目录只记录调度机制、作用域、失败策略和资源清理。

最小 Plugin 例子：

```python
import copy

from ftre.services.agent.hooks import AGENT_REQUEST_SPEC

async def apply(ctx):
    async def policy(payload, next_):
        config = await next_()
        config = copy.deepcopy(config)
        config.llm.temperature = min(config.llm.temperature, 0.2)
        return config

    receipt = ctx.hook_runtime.register(
        AGENT_REQUEST_SPEC,
        policy,
        owner="example-policy",
        context=ctx,
        global_listener=True,
    )
    ctx.effect(lambda: receipt.dispose, label="hook:agent:request:example-policy")
```

所有注册都挂在当前 Cordis Fiber；Plugin unload/restart 时由 Fiber Effect 撤销，
控制型 Hook 会等待已进入的 listener 归零后才完成清理。
