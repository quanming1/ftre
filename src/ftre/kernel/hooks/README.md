# ftre Kernel Hook 说明

`src/ftre/kernel/hooks` 是 ftre 对官方 `cordis-py` Events 的薄适配层。它解决的
问题是：Agent、Tool、Session 等业务模块需要在稳定的执行时机扩展行为，但核心
执行代码不应该直接知道“压缩插件”“标题插件”或“策略插件”的存在。

它**不是**消息队列、Service 容器或业务事件总线，也不保存 Prompt、Session、Tool
结果等业务状态。它只负责四件事：

1. 用 `HookSpec` 约束事件名称、输入/输出类型、调度模式和失败策略；
2. 将 Plugin listener 注册到 Cordis `Context`，并支持 `once`、`prepend`；
3. 触发时调用 Cordis 的串行、并行、瀑布等调度能力；
4. 在 Plugin/Fiber 卸载时取消监听器，等待正在执行的异步 listener 排空，并提供
   不包含用户 payload 的诊断快照。

## 一次 Hook 的完整流程

```text
业务 Owner 定义 HookSpec
        │ 例如 services.agent.hooks 定义 agent/before-run
        ▼
Plugin.apply(ctx) 注册 listener
        │ HookRuntime.register → Cordis Context.on/once
        ▼
核心代码走到扩展时机
        │ HookRuntime.dispatch(spec, payload, context=...)
        ▼
Cordis 按 HookMode 调度所有 listener
        │ listener 可以观察、修改 Waterfall 结果或中止流程
        ▼
返回结果给业务 Owner；卸载时由 Fiber Effect 自动 dispose
```

## 文件各自负责什么

| 文件 | 作用 | 不负责什么 |
| --- | --- | --- |
| `spec.py` | 从 `ftre-agent-core` 重新导出 Hook 契约类型，保证 Core/Gateway 使用同一协议 | 不重新实现 `HookSpec` |
| `runtime.py` | 注册、触发、失败处理、in-flight 计数和诊断 | 不定义业务 Hook 名称 |
| `scope.py` | 为 Agent 生成生命周期身份，并创建 Cordis isolate Context | 不维护第二套事件系统 |
| `diagnostics.py` | 定义失败和监听器状态的不可变快照 | 不记录 payload 或异常对象 |
| `__init__.py` | 提供稳定的公共导入面 | 不拥有业务行为 |
| `README.md` | 解释 Kernel Hook 的边界和使用方式 | 不替代各业务 Owner 的 Hook 文档 |

## HookSpec 的 Owner 规则

Kernel 不维护业务 Hook 名称总表。哪个模块定义了语义，哪个模块就是 Owner：

- Agent Hook 由 `services/agent/hooks.py` 定义；
- Session Hook 由 `services/session/hooks.py` 定义；
- Tool/LLM Hook 由 Agent Core 或对应 Service 定义；
- Inbox、Compaction 等业务能力由各自 Package 定义；Kernel 不感知当前 Gateway 是否必选 Inbox。

Kernel 只知道“如何调度一个合法的 HookSpec”，不应该出现 `compact`、`pending`、
`session_title` 等产品判断。这样卸载一个可选 Plugin 时，核心不需要增加空实现或
兼容分支。

## 调度模式怎么理解

`HookMode` 由 `ftre-agent-core` 定义，Runtime 只做映射：

| 模式 | 直观含义 | 典型用途 |
| --- | --- | --- |
| `EMIT` | 广播通知，不收集返回值 | 日志、指标、审计 |
| `PARALLEL` | 多个 listener 并发执行，统一等待完成 | 独立的异步观察/准备工作 |
| `SERIAL` | 按注册顺序逐个执行 | 必须保持顺序的副作用 |
| `BAIL` | 按 Cordis 规则尽早得到可用结果 | 多个候选者竞争处理 |
| `WATERFALL` | listener 通过 `next_()` 串成处理链 | 修改配置、拦截或继续 Agent 流程 |

`prepend=True` 只表达“先于已有 listener 注册”，不引入任意数字优先级；`once=True`
表示 Cordis 调用一次后自动失效。

## 最小使用示例

下面的 Plugin 在 Agent Run 准入时拒绝不满足策略的输入。它不直接调用 AgentLoop，也
不创建第二个事件总线，只通过公开的 HookSpec 和当前 Cordis Context 工作：

```python
import copy

from ftre.services.agent.hooks import AGENT_BEFORE_RUN_SPEC, RejectRun


async def apply(ctx):
    async def policy(payload, next_):
        # Waterfall listener 可以在 Run 创建前拒绝；允许时继续交给后续 listener。
        if payload.context.get("maintenance"):
            return RejectRun("Agent 正在维护")
        return await next_()

    receipt = ctx.hook_runtime.register(
        AGENT_BEFORE_RUN_SPEC,
        policy,
        owner="example-policy",
        context=ctx,
    )
    # HookRuntime 已将 receipt 绑定到当前 Plugin Fiber；无需再注册第二个 disposer。
```

## Agent 作用域为什么不用字符串

```text
Agent id = "default"（字符串） ──┐
                                ├─ 可能被重启复用，不能当唯一身份
Agent 生命周期对象 identity ────┘
                 │
                 ▼
HookScopeCarrier → Cordis isolate Context → 只接收本 Agent/祖先的 Hook
```

父 Scope 可以命中子 Scope，兄弟 Scope 不会串扰；同一个 Agent id 重建出来的新
生命周期也不会收到旧监听器。`scope` 字符串只用于日志展示，真正匹配使用对象
identity。

## 失败与卸载语义

- `PROPAGATE`：listener 失败先写入脱敏诊断，再把异常交回核心流程，适合控制型 Hook；
- `OBSERVE`：listener 失败先写诊断，然后不阻断主流程，适合日志/指标等旁观者；
- 卸载先从 Cordis 事件列表移除 listener，阻止新调用；已经开始的异步调用继续运行；
- Runtime 等待 `active_calls` 归零后才认为 Hook 真正排空，避免 Plugin 释放资源时
  还有异步回调在使用它们；
- Cordis Fiber Effect 是生命周期权威，Runtime 的注册表只是诊断镜像，不会另建
  一套清理机制。

消息队列、Session 持久化和公开 Service 不由 Hook 替代。Hook 只承担进程内的扩展
控制；需要保存状态或提供稳定能力时，仍应创建 Service，并通过 `inject` 使用。
