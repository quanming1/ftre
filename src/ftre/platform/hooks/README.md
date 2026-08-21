# ftre 语义 Hook 契约

`platform/hooks` 是 ftre 对官方 `cordis-py` Events 的窄适配，不拥有业务状态，也不
复制 Fiber 生命周期。Plugin 通过自己的 `Context` 注册监听器，卸载时由 Cordis
Effect 自动注销。

```python
from ftre.platform.hooks import HookMode, HookRuntime, HookSpec

spec = HookSpec(
    "agent/pre-step",
    "agent",
    HookMode.WATERFALL,
    payload_type=PreStepPayload,
    result_type=StepDecision,
    default=enter_step,
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

## 公共 Hook 清单

| Hook | Owner | 模式 | Payload → Result | 失败策略 / Scope | 副作用边界 |
|---|---|---|---|---|---|
| `agent/pre-step` | AgentLoop / Compaction | waterfall | `PreStepPayload → EnterStep/RejectStep` | propagate / Agent | 只能决定 pending 是否进入 Step；不得提前 claim |
| `agent/request` | AgentLoop / 路由 Plugin | waterfall | `AgentRequestPayload → AgentConfig` | propagate / Agent | 只能替换配置快照，不修改消息历史 |
| `agent/request-error` | AgentLoop / Recovery Plugin | waterfall | `RequestErrorPayload → RetryRequest/None` | propagate / Agent | 只有产生持久进展才允许 Retry |
| `agent/turn-stopping` | Agent Core / Agent Policy | waterfall | `TurnStoppingPayload → StopTurn/ContinueTurn` | propagate / Agent | finalize 前停止决策；continuation 必须有界 |
| `agent/turn-stopped` | AgentLoop | emit | `TurnStoppedPayload → None` | observe / Agent | finalize 后只读通知 |
| `agent/created`, `agent/disposed`, `agent/status`, `agent/session-start`, `agent/error` | AgentLoop | emit | `AgentLifecyclePayload → None` | observe / Agent | 只观察生命周期，不改变 Registry |
| `agent/inbox/inserted`, `claimed`, `discarded` | SessionLane | emit | `AgentInboxPayload → None` | observe / Agent | 只在对应 mailbox mutation 后通知 |
| `tools/pre-execute`, `tools/execute`, `tools/post-execute`, `tools/result` | Tool Adapter | waterfall / emit | `Tool*Payload → Tool*Result` | 见各 Spec / Agent | 不伪造 Tool call identity |
| `session/created`, `session/disposed`, `session/event` | SessionService | emit | `SessionLifecycle/EventPayload → None` | observe / global | 事实提交后通知，不回滚持久化 |
| `session/flush` | SessionService | parallel | `SessionFlushPayload → None` | propagate / global | 唯一持久化屏障，调用者必须走 `SessionService.flush()` |
| `system-prompt/assemble` | SystemPromptService | waterfall | `PromptAssemblyPayload → PromptAssembly` | propagate / Agent | 只替换结构化 assembly |
| `llm/stream` | Agent Core Adapter | waterfall | `LLMStreamPayload → AsyncIterator` | propagate / Agent | 保持请求 identity、取消和流结束语义 |

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
