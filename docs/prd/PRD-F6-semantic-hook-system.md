# PRD-F6-官方 cordis-py 运行时与语义 Hook 系统

> 历史阶段说明：F6 的 Core Bridge 方案已由 F7/C1 的跨仓库直接 Dispatcher 集成取代。
> F6 的验收记录保留用于回溯；当前 Core Hook Owner、契约和迁移边界以
> `PRD-F7-agent-core-hook-integration.md` 为准。

> 本 PRD 的第一道门禁是删除 ftre 自研的简化 `src/cordis`，接入并验证 `E:\cordis-py` 的官方 `cordis-py 0.4.0`。在此基座上，Hook 才定义为进程内、类型化、可作用域路由、受 Plugin Fiber 生命周期管理的扩展协议。Hook 不替代 Inbox、SessionEvent 或公开 Service，也不把 AgentLoop 变成新的全局事件总线。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F6 |
| 名称 | 官方 cordis-py 运行时与语义 Hook 系统 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | — |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` 阶段 F6；`AGENTS.md`；`docs/PROCESS.md`；`docs/prd/访谈.md`；PRD-F1 至 PRD-F5 |
| 官方基座 | `E:\cordis-py`；版本 `0.4.0`；安装后 `cordis.__file__` 必须来自已安装 distribution，不得来自 ftre 源码树 |
| 参考实现 | `E:\deepseek-harness\vendor\cordis`；`packages/core/agent`；`packages/core/agent-loop`；`packages/core/tools`；`packages/core/session`；`packages/compaction/compaction-basic` |

## 1. 背景与目标

### 1.1 当前问题

F1-F5 已完成四层目录、Composition Root、Service/Plugin 生命周期、旧目录清理与 Schedule Owner 收敛，但 F1 实际留下了一个必须先纠正的基座问题：`src/cordis` 是 Agent 临时编写的简化版本，并没有真正使用 `E:\cordis-py`。此外 Agent 数据面仍存在以下结构性问题：

| 当前事实 | 架构问题 | 直接后果 |
|---|---|---|
| `services/agent_loop/runtime/hooks.py` 仍定义 `agent/before_messages_build`、`agent/before_run` | Hook 数量少不是问题，问题在于它们是围绕旧实现细节命名的可变 Filter，不是稳定状态机语义 | Plugin 必须理解 TurnExecutor 内部消息构造顺序 |
| Hook payload 包含大量 `Any`、可变 `dict`、可变 `list` | 缺少输入/输出契约、所有权和合法变更范围 | Plugin 可意外破坏其他 Plugin 或持久化事实 |
| `Context.waterfall()` 是顺序 reducer | 不支持 `next()`、短路、around middleware 和默认实现 | 无法表达压缩恢复、模型路由、Tool 包装等 DSH 式扩展 |
| ftre 的 `src/cordis` 与 `E:\cordis-py` 同名 | Python import 优先命中仓库内简化包，pyproject 虽声明依赖但运行时没有使用成熟实现 | 依赖声明、实际运行时和 PRD 目标不一致；后续 Hook 开发会继续建立在错误内核上 |
| Hook 监听器没有 Agent scope | 全局监听器无法安全表达每 Agent、父子 Agent 或 Preset 级策略 | 多 Agent 场景容易串扰，只能在回调内手工判断 id |
| AgentLoop 直接持有 Compaction、Command、Tool、Profile 等具体对象 | Agent runtime 同时承担状态机、策略和产品行为 | `services/agent/` 体积持续膨胀，Feature 不能独立卸载 |
| Gateway 手工创建并向 ReActAgent 传入 `FtreCoreHookManager` | Gateway Hook 与 Agent Core Hook 是两套独立、无 scope、无 Fiber owner 的链 | Plugin 无法只依赖一个稳定 Hook 协议，卸载和失败语义不统一 |
| Hook、消息队列和 Session 持久事件边界不清 | 临时控制信号和持久事实可能混用 | pending 恢复、崩溃语义和 Plugin 重放行为难以证明 |
| Hook 失败没有按语义分类 | 观察者异常、策略拒绝、持久化失败可能被同等处理 | 要么错误被吞掉，要么非关键 Plugin 破坏整个循环 |

DSH 的核心做法不是建立一个拥有所有业务的 `HookManager`，而是：

1. Cordis 提供通用的 `emit`、`parallel`、`serial`、`bail`、`waterfall` 调度语义。
2. Agent、Tool、Session、System Prompt 各自声明自己拥有的语义 Hook。
3. AgentLoop 只在稳定状态机边界触发 Hook，不直接依赖 Compaction、Retry、Checkpoint 等 Plugin。
4. Plugin 通过 Agent scope 注册 Hook，并由 Fiber 自动卸载。
5. Inbox 只保存未来输入；SessionEvent 保存持久事实；Hook 只负责进程内扩展控制。

### 1.2 目标

本阶段完成后，ftre 必须先真正使用 `E:\cordis-py` 的官方实现，再具备一个可供内置及外部 Plugin 使用的稳定语义 Hook 系统：

- ftre 不再包含 `src/cordis`；运行时 `cordis.__file__` 来自 `cordis-py 0.4.0` 安装 distribution。
- ftre 只使用官方 Cordis 的 `Context`、`Fiber`、`Service`、`Inject`、`Effect`、`Events` 及其正式生命周期 API。
- `kernel/plugins` 只做 Manifest、Discovery、Loader、Manager、Diagnostics 薄适配，不复制 Cordis 状态机。
- Cordis Runtime 提供真正的 continuation waterfall 与明确的五种调度模式。
- Hook 契约类型化、可诊断，并声明所属领域、调度模式、默认行为和失败策略。
- 每个 Agent 拥有独立 Hook scope，父子 Agent scope 可继承，全局 Plugin 可显式观察全部 Agent。
- Agent Service 只拥有 Agent Registry、Agent Protocol、Inbox 和 Agent Hook 契约；AgentLoop 成为独立 Provider。
- AgentLoop 只负责 Turn/Step 状态机和 Hook 触发，不再直接拥有 Compaction 或 Command 产品策略。
- Compaction 成为第一个完整消费 `agent/pre-step` 与 `agent/request-error` 的 Feature Plugin。
- Tool、Session、System Prompt 拥有各自 Hook 管线，避免所有扩展点挤入 Agent Hook。
- 旧 `agent/before_messages_build`、`agent/before_run` 及 reducer `filter` 使用全部删除，不保留兼容入口。

### 1.3 成功状态

一个新的行为 Plugin 应能只依赖公开 Service 与 Hook 契约完成如下能力，而不 import AgentLoop、TurnExecutor、Session 存储或其他 Feature 私有模块：

```python
def apply(ctx: Context, config=None):
    async def compact_before_step(payload, next_):
        await ctx.compaction.compact_if_needed(payload.agent, payload.signal)
        return await next_()

    ctx.on(AGENT_PRE_STEP, compact_before_step)
```

卸载该 Plugin 后，监听器和其派生资源必须被清理；AgentLoop 应继续运行，且无需条件分支判断该 Plugin 是否存在。

### 1.4 非目标

- 不修改 Desktop、Web 客户端或其他仓库。
- 不在本阶段重写 `ftre-agent-core` 的 ReAct/LLM 算法。
- 不修改 `E:\cordis-py`；只在 ftre 中接入其已发布源码/Distribution，并通过正式公开 API 适配。
- 不把所有 Session 存储一次性迁移为完整 Event Sourcing；本阶段只建立 Hook 与持久事实的边界及必要接口。
- 不把每个内部函数都暴露成 Hook；只开放稳定、可长期维护的语义边界。
- 不用 Hook 替代公开 Service 的查询和命令接口。
- 不用 Hook 替代 Inbox 队列或 SessionEvent 持久日志。
- 不承诺旧 Hook 名称、旧 payload 或旧外部插件兼容；迁移完成后直接删除旧入口。
- 不以 `PluginContext`、`context.settle()`、`context.unload()`、`fiber.missing/error/provided` 等 ftre 简化内核私有属性冒充官方 Cordis API。
- 不在 F6 顺带重构无关的 Agent Profile 配置格式、HTTP DTO 或客户端展示。

## 2. 核心概念与设计原则

### 2.1 四种不同的协作载体

| 载体 | 作用 | 是否持久化 | 是否允许改变控制流 |
|---|---|---:|---:|
| Service | 显式查询、命令和状态能力 | 由 Service 自己决定 | 是，调用方显式等待结果 |
| Hook | Plugin 间进程内扩展协议 | 否 | 由 Hook 模式决定 |
| Inbox | 尚未被 Agent 消费的未来输入 | 是 | 由 Agent 状态机领取 |
| SessionEvent | 已经发生的持久事实 | 是 | 否，事实提交后只能追加或按正式替换协议演进 |

任何实现不得把 ToolResult、Command、Compaction 生命周期事件重新塞入 Inbox；只有未来需要 Agent 消费的输入才能进入 Inbox。

### 2.2 Hook 所有权

- Agent 领域拥有 `agent/*` Hook。
- Tool 领域拥有 `tools/*` Hook。
- Session 领域拥有 `session/*` Hook。
- System Prompt 领域拥有 `system-prompt/*` Hook。
- LLM Provider/Router 领域拥有 `llm/*` Hook。
- Feature 只能消费这些 Hook，不得在其他领域私有实现中插入回调。

### 2.3 稀疏语义边界

Hook 必须位于状态机中长期稳定的边界，例如“拟进入 Step”“模型请求失败”“Tool 即将执行”。以下内部细节禁止成为公共 Hook：

- 某个私有 helper 开始或结束。
- 某个临时 `dict` 构造完成。
- 特定 Provider 的内部 token 计算过程。
- 单一 Feature 私有算法的中间阶段。

### 2.4 默认行为属于 Hook 契约

每个控制型 Hook 必须声明无监听器时的默认行为。例如：

```text
agent/pre-step       → EnterStep(candidate_messages)
agent/request        → 当前 Agent 的冻结请求配置
agent/request-error  → None（错误终止）
tools/pre-execute    → Allow
tools/execute        → 执行 Tool Body
tools/post-execute   → Accept 原结果
system-prompt/assemble → 返回基础 PromptAssembly
```

Plugin 只有调用 `next()` 才会委托给后续监听器及默认行为；不调用表示明确接管或短路。

## 3. 需求范围

### 3.0 官方 cordis-py 前置门禁

- [x] PRE1：`pyproject.toml` 必须锁定 `cordis-py==0.4.0`，本地开发文档必须明确先安装/链接 `E:\cordis-py`；不得通过 `src/cordis`、`sys.modules` 或 import fallback 阻断官方包。
- [x] PRE2：删除 `src/cordis/__init__.py` 及其整个本地同名包；ftre 源码树中不得出现第二套 `cordis` 实现。
- [x] PRE3：所有 Plugin 入口的类型注解和运行时对象统一使用官方 `cordis.Context`；不得继续导入不存在于官方包的 `PluginContext`、`ServiceAccessError` 等简化 API。
- [x] PRE4：`kernel/plugins` 适配官方 `Context.plugin()`、`Fiber.await_()`、`Fiber.dispose()`、`Fiber.restart()`、`Fiber.state` 和 `Context.registry` 等真实 API；不得向官方 Context 传递 `id`、`parent` 等不存在参数。
- [x] PRE5：Plugin Loader 的 required/optional、PENDING/ACTIVE/FAILED、依赖重载和卸载诊断必须基于官方 Fiber/Registry 状态，不得在 ftre 再复制 Fiber 状态机。
- [x] PRE6：官方 Cordis 的 `EventsService`、`Context.on/once/emit/parallel/serial/bail/waterfall` 必须由 ftre 直接消费；当前 Hook/Filter 迁移不得建立第三套 EventHub。
- [x] PRE7：生产导入、测试和 Gateway 启动必须证明 `cordis.__file__` 不位于 `E:\ftre\src\cordis`，且 `importlib.metadata.version("cordis-py") == "0.4.0"`。
- [x] PRE8：F6 变更只发生在 `E:\ftre`；不得修改、复制回写或生成提交到 `E:\cordis-py`。
- [x] PRE9：不允许为了保持旧测试而在 ftre 恢复 `PluginContext`、`Context.settle/unload` 或 `_failed_fiber` 伪造对象；测试必须改为官方 Fiber 生命周期。
- [x] PRE10：完成官方 Cordis API 契约测试后，才能开始 F6 后续语义 Hook、Agent scope 和 Compaction 迁移任务。

### 3.0.1 官方发行物切换门禁（后置任务，不阻塞 Hook 开发）

F6.1/F6.2 已经使用 `E:\cordis-py` 的官方源码 distribution 完成基座验证。Hook
开发阶段继续使用该官方源码 distribution；PyPI 发布只影响最终交付和 CI 安装方式，
不阻塞 F6.3-F6.11 的 Hook 实现、测试和本地验收。以下门禁在需要公开发行 ftre
运行环境之前执行：

- [ ] PRE11：`E:\cordis-py` owner 在其仓库生成 sdist 和 wheel，执行 `twine check`，并确认包内只有预期的 `cordis` 模块、元数据和许可证文件。
- [ ] PRE12：将发行物上传 TestPyPI，在全新 Python 3.12 虚拟环境中安装；验证 `import cordis`、`importlib.metadata.version("cordis-py")`、Context/Fiber 生命周期和 Events 契约。
- [ ] PRE13：正式 PyPI 项目 `cordis-py` 成功占用并发布目标版本。已发布版本不可覆盖；若 0.4.0 发行物需要修改，必须递增到 0.4.1 或更高版本，并同步 ftre pin。
- [ ] PRE14：ftre 在无 `E:\cordis-py`、无本地 `src/cordis` 的干净环境中执行 `pip install cordis-py==<目标版本>` 后，通过 import-origin、metadata、全量测试和 Gateway smoke。
- [ ] PRE15：ftre CI 改为从 PyPI 安装 `cordis-py`，删除对 `quanming1/cordis-py` sibling checkout 的依赖；README 仅保留“开发 cordis-py 本身时才使用 sibling path”的说明。
- [ ] PRE16：发布采用 PyPI Trusted Publishing 或项目级最小权限 token；仓库、日志和测试输出不得包含 PyPI 密钥、恢复码或用户凭据。

PRE11-PRE13 是 `E:\cordis-py` owner 的外部发布动作，本仓库只记录验收证据，不修改该仓库；PRE14-PRE16 是 ftre 侧的安装、CI 和安全收尾。它们不影响 F6.3-F6.11 的开发顺序。

### 3.1 Hook Runtime

- [x] FR1：Cordis 事件系统必须支持 `emit`、`parallel`、`serial`、`bail`、`waterfall` 五种明确模式，且模式行为与文档及测试一致。
- [x] FR2：`waterfall` 必须采用 continuation middleware 语义；监听器接收 `next_`，可在下游前后执行、修改下游结果或不调用 `next_` 直接短路。禁止继续使用当前顺序 reducer 语义冒充 waterfall。
- [x] FR3：`emit` 必须对每个观察型监听器独立容错；同步异常与异步拒绝均记录诊断，不能阻止后续观察者或回滚已提交事实。
- [x] FR4：`parallel` 必须启动所有匹配监听器、等待全部 settled，并在任一失败时抛出包含全部失败的聚合异常。
- [x] FR5：`serial` 必须按确定顺序逐个等待；支持第一个有效 bail 值停止后续监听器。对返回 `None` 的屏障 Hook 必须完整执行全部监听器。
- [x] FR6：`bail` 必须同步按序返回第一个非 `None`、非 `False` 的结果。
- [x] FR7：Hook 注册必须归属于调用 Plugin 的 Fiber；正常卸载、启动回滚和热重载均自动注销监听器。注销后不得进入新的 dispatch，已经进入的控制型 dispatch 必须可追踪并达到 quiescence 后再完成卸载。
- [x] FR8：Hook 顺序默认采用 Composition/注册顺序；只允许显式 `prepend` 改变头部顺序，不引入难以诊断的任意数字优先级。
- [x] FR9：官方 `Context` 必须公开类型一致的 `on`、`once`、`emit`、`parallel`、`serial`、`bail`、`waterfall` 能力，且所有注册自动绑定当前 Fiber。

### 3.2 类型契约与作用域

- [x] FR10：每个公共 Hook 必须拥有稳定名称、所属领域、调度模式、类型化 payload/result、默认行为、失败策略和是否 scope-filtered 的 HookSpec；禁止公共 Hook 使用裸 `Any` payload。
- [x] FR11：Hook payload 默认使用冻结 dataclass、Protocol、Enum、tuple 或只读 Mapping；只有契约明确允许的字段可以通过返回值替换，监听器不得原地修改其他 Plugin 可见输入。
- [x] FR12：必须提供 Agent scope：全局监听器可接收全部 Agent；Agent scope 监听器只接收该 Agent；父 Agent scope 监听器可接收其后代 Agent；无关兄弟 scope 互不串扰。
- [x] FR13：必须提供等价于 DSH `agentEvents()` 的融合分发器，将 scope carrier 与 payload 中的 Agent 主体绑定；调用方不能构造 scope 属于 Agent A、payload 却属于 Agent B 的分发。
- [x] FR14：作用域采用运行时对象身份比较，不以可复用字符串 id 作为唯一隔离依据；Agent 生命周期结束后旧 scope 不得命中新建的同 id Agent。
- [x] FR15：Hook Runtime 必须输出诊断信息，包括 Hook 名称、模式、Plugin/Fiber owner、scope、监听顺序、当前活跃调用数和最近失败；诊断不得泄露完整 Prompt、Tool 参数或用户敏感内容。

### 3.3 Agent 契约与状态机 Hook

- [x] FR16：`services/agent` 必须收敛为 Agent Protocol、AgentRegistry、Inbox、scope/dispatch 和 `agent/*` Hook 契约；具体 React/Turn/Step 驱动迁入独立 `services/agent_loop` Provider，Agent Service 不暴露内部 Loop 对象。
- [x] FR17：Agent 生命周期必须发布 `agent/created`、`agent/disposed`、`agent/status`、`agent/session-start` 和 `agent/error` 观察型 Hook；观察者失败不得破坏 Agent Registry 的成对生命周期。
- [x] FR18：Inbox 必须发布 `agent/inbox/inserted`、`agent/inbox/claimed`、`agent/inbox/discarded` 观察型 Hook；Hook payload 必须引用准确 Agent、消息和领取所属 Turn。
- [x] FR19：必须实现 `agent/pre-step` waterfall。它接收 Agent、候选输入、拟进入的 Turn/Step 和当前取消信号，返回 `EnterStep(messages)` 或类型化 `RejectStep(disposition, reason)`。
- [x] FR20：ftre 的 `agent/pre-step` 必须在 pending 正式领取前运行；压力压缩或 Hook 失败期间候选消息仍保持 pending。只有 `EnterStep` 成功后才能执行领取和消息提交；`RejectStep` 必须通过显式 disposition 决定保留或丢弃，禁止隐式丢消息。
- [x] FR21：同一 Agent 的候选快照、Hook 决策和最终领取必须受同一个 SessionLane/Agent driver 串行所有权保护；并发新消息只能留给当前 Step 或下一 Turn 的既定队列目标，不得被错误领取。
- [x] FR22：必须实现 `agent/request` waterfall，只允许替换冻结的模型请求配置；模型可见消息必须通过 Session/Prompt 正式通道进入，禁止在该 Hook 中修改消息历史。
- [x] FR23：必须实现 `agent/request-error` waterfall；默认结果为终止原错误，处理方只有在完成可证明的恢复动作后才能返回 `RetryRequest`。每次恢复必须有次数上限或由策略明确声明无限重试。
- [x] FR24：必须实现 `agent/turn-stopping` serial 屏障。当前 F6 提供稳定的串行收尾屏障；全部监听器结束后 Turn 才能完成。`agent.steer()` 的业务输入 API 不属于本阶段数据面，保留为后续 Agent continuation 阶段。
- [x] FR25：每个 Turn 拥有独立取消信号；所有控制型 Agent Hook 必须接收并遵守该信号。Hook 不得保存旧 Turn 信号并用于控制后续 Turn。
- [x] FR26：控制型 Hook 异常只能终止或拒绝当前 Step/Turn，并通过 `agent/error` 报告；Agent driver 必须收敛回可继续接收输入的稳定状态，除非 Agent 正在 dispose。

### 3.4 Tool、Session、Prompt 与 LLM Hook

- [x] FR27：Tool Runtime 必须实现 `tools/pre-execute → guard → tools/execute → Tool Body → tools/post-execute → finalize → tools/result` 管线，并为每个阶段提供类型化输入输出。
- [x] FR28：`tools/pre-execute` 只允许返回 Allow、Deny 或 Ask，禁止原地改写已记录的 Tool 名称与参数；Owner Guard 的拒绝不得被后续 Plugin 重新放行。
- [x] FR29：`tools/execute` 必须是 around waterfall，用于超时、重试、指标和取消信号包装；包装器不得改变 Tool call identity，且必须等待已启动 Tool 达到 quiescence。
- [x] FR30：`tools/post-execute` 可接受、替换、补充上下文或阻止结果；`tools/result` 只能观察最终冻结结果，观察者失败不得改变返回给 Agent 的结果。
- [x] FR31：Session 必须提供 `session/created`、`session/disposed`、`session/event` 观察型 Hook，以及 `session/flush` parallel 持久化屏障。`session/event` 只在事实提交后通知，观察者失败不得撤销提交。
- [x] FR32：Session 的所有 flush 必须经过唯一公开 Service 方法分发，调用方不得自行 raw-dispatch `session/flush`，确保 scope、错误和持久化语义一致。
- [x] FR33：System Prompt 必须提供 `system-prompt/assemble` waterfall；各 Plugin 通过结构化 PromptSection 贡献或转换内容，不得直接寻找并修改消息数组中的首个 system message。
- [x] FR34：LLM Runtime 必须提供 `llm/stream` around waterfall，允许路由、检查点、遥测和 Provider 包装，同时保持请求 identity、取消和流结束语义。

### 3.5 Compaction 与 Command 首批迁移

- [x] FR35：Compaction 必须迁入独立 `features/compaction` Owner，并通过公开 `CompactionService` 暴露 `compact_if_needed`、`compact_now` 和 overflow recovery；AgentLoop、Agent Service、Command 不得 import 其私有实现。
- [x] FR36：自动压力压缩必须监听 `agent/pre-step`，在 pending 仍未领取时工作；正常压力压缩失败默认记录告警并调用 `next_()` 继续，不得静默吞掉取消。
- [x] FR37：上下文溢出恢复必须监听 `agent/request-error`；只有压缩产生可证明的持久上下文进展且未取消时才能短路后续监听器并返回 `RetryRequest`，否则委托 `next_()` 保留原始错误。
- [x] FR38：手动压缩必须通过 Command Plugin 调用 `CompactionService.compact_now(agent)`；Agent 进入独占 maintenance 状态，期间允许新输入入队，maintenance 结束后由 pending 唤醒 driver。
- [x] FR39：Command 必须继续在接入层解析并由 CommandService 执行，命令文本不得进入 Agent Inbox 或模型消息；命令只通过公开 Agent/Compaction Service 工作，不得依赖 AgentLoop、TurnExecutor 或 CompactManager。
- [x] FR40：命令执行结果和 Compaction 生命周期如需持久化，必须使用各自结构化 SessionEvent；不得使用临时 Hook 作为持久记录。

### 3.6 清理、文档与门禁

- [x] FR41：删除 `agent/before_messages_build`、`agent/before_run`、`MessagesBuildContext`、`AgentRunContext`、`append_to_first_system` 以及生产代码中的 `Context.filter()` Hook 用法，不保留别名或双轨触发。
- [x] FR42：架构测试必须禁止 Feature import `services.agent_loop` 私有模块，禁止 AgentLoop import `features.compaction`，禁止 Command import AgentLoop/TurnExecutor/Compaction 私有实现。
- [x] FR43：每个公共 Hook 必须在开发文档中列出 owner、模式、payload/result、默认行为、失败策略、scope 和允许的副作用，并提供最小 Plugin 示例。
- [x] FR44：Gateway 启动、Plugin unload/restart、Agent dispose 和进程关闭必须等待控制型 Hook、Tool 和持久化屏障达到既定 quiescence；不得泄漏 Task、监听器或资源。
- [x] FR45：本阶段不得新增旧路径兼容模块、aggregate 导入或空转发壳；旧 Hook 使用迁移完成后直接删除。
- [x] FR46：F6 只修改 `E:\ftre`。对 `ftre-agent-core` 现有 ReAct/Tool Hook 的接入必须由 `infrastructure/agent_core` 适配器完成；产品 Plugin 不得直接注册 `FtreCoreHookManager`，也不得要求修改 `E:\ftre-agent-core` 才能通过验收。

### 3.7 非功能需求

- **正确性**：同一 Agent 最多一个 active turn；Hook 不得突破 SessionLane 串行化和 maintenance 互斥。
- **可诊断性**：每个控制型 Hook 失败包含 Hook、owner、Agent/Session identity 和阶段坐标，但日志不得包含完整用户内容。
- **可卸载性**：Plugin unload 后无新 Hook 调用进入该 Plugin；进行中的调用按契约结束后再完成资源清理。
- **确定性**：相同 Composition 和输入产生相同监听顺序；测试不得依赖哈希或文件系统枚举顺序。
- **性能**：无监听器时 Hook 快路径不得创建后台 Task；Agent 热路径可缓存融合 dispatcher 和 scope carrier。
- **安全性**：Tool Hook 不能伪造 call identity、绕过 Owner Guard 或将已拒绝调用重新放行。
- **兼容性**：保持现有 Gateway HTTP/WS 对客户端的外部协议与主要行为；不兼容范围仅限内部 Python Hook API。

## 4. 目标结构与技术方案

### 4.1 目标文件树

```text
pyproject.toml                     # cordis-py==0.4.0；不再打包 src/cordis
src/
└─ ftre/
   ├─ platform/
   │  └─ hooks/
   │     ├─ __init__.py
   │     ├─ spec.py                # HookSpec、HookMode、FailurePolicy
   │     ├─ scope.py               # HookScope、父子链、scope carrier
   │     └─ diagnostics.py         # owner/scope/order/failure 诊断
   │
   ├─ services/
   │  ├─ agent/
   │  │  ├─ service.py             # AgentRegistry 公共 Service
   │  │  ├─ contracts.py           # AgentDriver/Registry Protocol
   │  │  ├─ registry.py             # Agent identity 与 scope Registry
   │  │  ├─ agent.py               # Agent Protocol 与 maintenance/send API
   │  │  ├─ inbox.py               # next-turn / next-step 与 pending 事实
   │  │  ├─ hooks.py               # agent/* HookSpec 与 payload/result
   │  │  ├─ dispatch.py            # Agent + scope 融合分发器
   │  │  └─ plugin.py              # Agent Service Provider
   │  │
   │  ├─ agent_loop/
   │  │  ├─ provider.py             # AgentLoop 唯一构造 Provider
   │  │  ├─ driver.py               # AgentLoop → AgentDriver 端口
   │  │  ├─ runtime/                # 私有 Loop/Lane/Mailbox/Compaction 算法
│  │  │  ├─ loop/
│  │  │  ├─ mailbox/
│  │  │  └─ (only runtime orchestration; semantic hooks live in platform/hooks)
   │  │  ├─ turn.py                # Turn 边界
   │  │  ├─ step.py                # pre-step/request/request-error
   │  │  ├─ tool_calls.py          # Tool 调度，不拥有 Tool 策略
   │  │  └─ plugin.py
   │  │
   │  ├─ tools/
   │  │  ├─ hooks.py               # tools/* HookSpec
   │  │  ├─ service.py
   │  │  └─ plugin.py
   │  │
   │  ├─ session/
   │  │  ├─ hooks.py               # session/* HookSpec
   │  │  ├─ service.py
   │  │  └─ plugin.py
   │  │
   │  ├─ system_prompt/
   │  │  ├─ hooks.py
   │  │  └─ service.py
   │  │
   │  ├─ llm/
   │  │  └─ hooks.py
   │  │
   │  └─ command/
   │     ├─ service.py
   │     └─ plugin.py
   │
   ├─ infrastructure/
   │  └─ agent_core/
   │     ├─ hook_bridge.py          # Core Hook ↔ ftre 领域 Hook 单向适配
   │     ├─ model_adapter.py        # 在 ftre 边界提供 llm/stream 包装点
   │     └─ tool_adapter.py         # 在 ftre 边界提供 Tool 管线包装点
   │
   └─ features/
      └─ compaction/
         ├─ service.py
         ├─ policy.py
         ├─ summarizer.py
         ├─ hooks.py               # 自动 pressure / overflow 监听器
         ├─ command.py             # /compact 注册
         └─ plugin.py

tests/
├─ hooks/                          # 调度模式、作用域、错误、quiescence
├─ contracts/                      # Hook payload/result 与 Service 契约
├─ architecture/                   # 依赖方向和旧 Hook 禁止项
├─ lifecycle/                      # load/unload/restart/in-flight
├─ services/
│  ├─ agent/
│  ├─ agent_loop/
│  ├─ tools/
│  └─ session/
└─ features/
   └─ compaction/
```

文件可以在实现中因现有代码规模做小幅合并，但 owner、依赖方向和公共/私有边界不得改变；任何偏离必须进入本 PRD 变更记录并重核相关 AC。

### 4.2 依赖方向

```text
installed cordis-py (official Context/Fiber/Events)
        ↓
platform.hooks
        ↓
services.agent contracts ← services.agent_loop provider
        ↑                           ↓
        ├──── features.compaction ──┤
        ├──── features.skill ───────┤
        └──── session checkpoint ───┘

services.tools    拥有 tools/* Hook
services.session  拥有 session/* Hook
system_prompt     拥有 system-prompt/* Hook
services.command  只调用公开 Service
agent_core bridge 只负责把外部算法库调用映射到上述公开 Hook
```

禁止方向：

```text
services.agent              → features.compaction
services.agent_loop         → features.compaction
services.command            → services.agent_loop 私有模块
feature                     → 另一个 feature 私有模块
Hook payload                → TurnExecutor 私有类型
```

### 4.3 Hook Runtime 模型

```python
class HookMode(StrEnum):
    EMIT = "emit"
    PARALLEL = "parallel"
    SERIAL = "serial"
    BAIL = "bail"
    WATERFALL = "waterfall"


@dataclass(frozen=True)
class HookSpec(Generic[P, R]):
    name: str
    owner: str
    mode: HookMode
    payload_type: type[P]
    result_type: type[R] | None
    failure_policy: FailurePolicy
    scoped: bool = False
```

HookSpec 是公共契约和诊断事实源。调用者不得用同一名称以不同模式分发；payload/result 在开发与测试环境必须执行边界校验。

### 4.4 Continuation Waterfall

逻辑模型：

```python
async def waterfall(spec, payload, default):
    listeners = snapshot_matching_listeners(spec)

    async def invoke(index):
        if index == len(listeners):
            return await default()
        return await listeners[index](payload, lambda: invoke(index + 1))

    return await invoke(0)
```

执行顺序：

```text
Plugin A before
  Plugin B before
    Default behavior
  Plugin B after
Plugin A after
```

监听器不调用 `next_()` 时，后续 Plugin 和默认行为均不执行。重复调用同一个 `next_()` 必须被 Runtime 拒绝，防止同一 Tool、请求或 Step 被执行两次。

### 4.5 Agent Scope

每个 Agent 创建一个不可复用的 scope identity，并可声明父 scope：

```text
global
  └─ root-agent scope
       ├─ coding-subagent scope
       └─ review-subagent scope
```

事件向祖先传播，不向无关分支传播：

```text
coding-subagent dispatch
  → global listener
  → root-agent scoped listener
  → coding-subagent scoped listener
  ✗ review-subagent scoped listener
```

Agent 融合 dispatcher 自动补充 payload.agent，并以同一 Agent identity 生成 carrier。任何 scope/payload 主体不一致必须在 dispatch 前失败。

### 4.6 Agent 消息与 Step 流程

```text
Channel / HTTP 输入
  ↓
Input Router
  ├─ slash command → CommandService → command/run + command/done
  └─ agent input   → Agent.send/followup/steer
                         ↓
                     Inbox pending
                         ↓
                 单 Agent driver 获得所有权
                         ↓
                  peek candidate batch
                         ↓
              system-prompt/assemble waterfall
                         ↓
                 agent/pre-step waterfall
                  ├─ checkpoint
                  ├─ compaction pressure
                  ├─ skill/instructions
                  └─ policy
                         ↓ EnterStep
              claim + 持久消息边界提交
                         ↓
                    step/start
                         ↓
                  agent/request waterfall
                         ↓
                    llm/stream waterfall
                         ↓
                 Tool Hook pipeline（如有）
                         ↓
               additional context → next-step
                         ↓
                agent/turn-stopping serial
                         ↓
                      turn/end
```

候选消息在 `agent/pre-step` 成功前保持 pending。这样压力压缩、Prompt 组装或 Hook 故障不会制造“已经 claim、尚未写入聊天历史”的丢失窗口。

### 4.7 Agent Hook 契约

| Hook | 模式 | 默认行为 | 失败边界 |
|---|---|---|---|
| `agent/created` | emit | 通知全部观察者 | 单监听器隔离；Registry publication 保持一致 |
| `agent/disposed` | emit | 通知全部观察者 | 单监听器隔离；不得阻止 Registry detach |
| `agent/status` | emit | 发布状态迁移 | 重复状态由 invariant 报错 |
| `agent/session-start` | emit | 启动通知 | 不作为第一 Turn 的异步阻塞门禁 |
| `agent/inbox/inserted` | emit | 通知投影 | 事实提交后通知 |
| `agent/inbox/claimed` | emit | 通知投影 | 必须携带 owning turn |
| `agent/inbox/discarded` | emit | 通知投影 | 事实提交后通知 |
| `agent/pre-step` | waterfall | `EnterStep(candidate)` | 异常不 claim pending；当前提案失败，driver 可继续 |
| `agent/request` | waterfall | 冻结的当前请求配置 | 异常结束当前 Step，driver 收敛 |
| `agent/request-error` | waterfall | 不处理原错误 | 只有类型化 Retry 可接管 |
| `agent/turn-stopping` | serial | 无操作 | 异常结束当前 Turn，driver 收敛 |
| `agent/error` | emit | 观察错误 | 观察者失败被隔离 |

### 4.8 Tool Hook 契约

| Hook | 模式 | 允许行为 | 禁止行为 |
|---|---|---|---|
| `tools/pre-execute` | waterfall | Allow/Deny/Ask | 改写 call id/name/arguments；覆盖 Owner Guard deny |
| `tools/execute` | waterfall | 包装 signal、超时、重试、指标 | 改变 call identity；遗弃已启动 Tool Task |
| `tools/post-execute` | waterfall | Accept/Replace/Block/additional contexts | 修改已提交的 call 事实 |
| `tools/result` | emit | 观察冻结最终结果 | 修改 Agent 获得的结果；抛错影响调用方 |
| `tools/change` | emit | 通知目录变化 | 阻止注册或注销完成 |

### 4.9 Session 与持久化边界

```text
Session.append(event)
  1. 校验并冻结事件
  2. 提交到内存事实源
  3. emit session/event（post-commit、观察者隔离）

SessionService.flush(session)
  1. 解析准确 session scope
  2. parallel session/flush
  3. 等待全部 persistence listener settled
  4. 任一失败则聚合抛出
```

Hook 不进入 Session 重放日志。需要重放、恢复或客户端展示的事实必须使用 SessionEvent。

### 4.10 Compaction Plugin 接入

```text
自动压力：
agent/pre-step
  → CompactionService.compact_if_needed(agent, PRESSURE)
  → 成功/无需压缩：next_()
  → 非取消失败：告警后 next_()

上下文溢出：
agent/request-error
  → 判断标准 overflow code
  → 记录 surface generation
  → compact_if_needed(agent, OVERFLOW)
  → generation 前进：RetryRequest
  → 无进展/已取消/超限：next_()

手动压缩：
/compact Command Plugin
  → CompactionService.compact_now(agent)
  → agent.run_maintenance(job)
  → completion / flush
  → pending 自动唤醒
```

AgentLoop 不知道 Compaction Plugin 是否加载。未加载时，默认 Hook 行为使 Agent 正常运行，只是不执行自动压缩恢复。

### 4.11 失败策略

| 类别 | 策略 |
|---|---|
| post-commit 观察通知 | fail-contained：逐监听器记录，继续其他监听器 |
| 用户权限/策略 gate | fail-closed：不能确认时不执行有副作用操作 |
| 自动压力优化 | fail-open：记录告警，继续当前 Step；取消除外 |
| durable checkpoint | fail-closed：模型请求或 Tool 副作用不得越过失败屏障 |
| request recovery | 默认保留原错误；处理者只有完成恢复后返回 Retry |
| Tool body/wrapper | 规范化为 Tool error；已启动工作必须等待收敛 |
| Agent Hook 未知异常 | 结束当前 Step/Turn并报告，driver 本身继续可用 |
| Plugin unload | 阻止新调用进入，等待已进入的控制型 Hook 收敛 |

### 4.12 迁移策略

迁移采用单轨切换，不保留旧 Hook 兼容：

1. 先用测试冻结当前 Agent、Tool、Session、Command 和 Compaction 外部行为。
2. 建立新 Cordis Hook Runtime、HookSpec 和 scope，不接入生产路径。
3. 让 AgentLoop 在稳定边界触发新 Hook，并迁移现有内置监听器。
4. Tool、Session、System Prompt 切换到领域 Hook 管线。
5. 将 Compaction 迁出 Agent runtime，通过 Hook 和 Service 接入。
6. Command 改为只调用公开 Service。
7. 删除旧 Hook 常量、Filter、直接引用和临时桥接；架构门禁禁止回流。

每个切片完成后必须运行相关专项测试，且不得出现新旧 Hook 双触发的长期状态。

`ftre-agent-core` 是仓库边界外的无状态算法库，本阶段不修改它。迁移期间由
`infrastructure/agent_core/hook_bridge.py` 独占现有 `FtreCoreHookManager` 的注册：

```text
ftre Agent/Tool semantic hook
          ↕ typed adapter
ftre-agent-core on_turn_start/on_pre_tool/on_post_tool/on_stop/on_turn_end
```

Bridge 只做类型转换和生命周期转接，不重新定义产品策略。内置及外部 Plugin 只能注册
ftre 公共 Hook；`FtreCoreHookManager` 不作为 Service 暴露。模型与 Tool 的 around Hook
在 ftre 创建 ReActAgent、模型 handler 和 Tool definition 的适配边界完成，不能以修改外部
Agent Core 作为 F6 完成前提。

## 5. 公共接口草案

### 5.1 Agent Pre-Step

```python
@dataclass(frozen=True)
class AgentPreStep:
    agent: Agent
    candidate_messages: tuple[UserMessage, ...]
    turn: int
    step: int
    signal: AbortSignal


@dataclass(frozen=True)
class EnterStep:
    messages: tuple[UserMessage, ...]


@dataclass(frozen=True)
class RejectStep:
    disposition: Literal["keep_pending", "discard"]
    reason: str
```

### 5.2 Agent Request Recovery

```python
@dataclass(frozen=True)
class AgentRequestError:
    agent: Agent
    turn: int
    step: int
    provider: str
    failure: LlmFailure
    retry_policy: RetryPolicy | None
    signal: AbortSignal


@dataclass(frozen=True)
class RetryRequest:
    reason: str
```

`None` 表示未处理，保留原始请求错误。

### 5.3 Agent Protocol

```python
class Agent(Protocol):
    id: AgentId
    session: Session
    inbox: Inbox
    scope: HookScope

    def followup(self, message: UserMessage) -> None: ...
    def steer(self, message: UserMessage) -> None: ...
    def inject(self, message: UserMessage) -> None: ...
    def cancel(self, cause: AgentCancelCause, *, keep_inbox: bool = False) -> None: ...
    async def run_maintenance(self, job: MaintenanceJob[T]) -> T: ...
    async def when_idle(self) -> None: ...
```

公开 Agent Protocol 不暴露 TurnExecutor、CompactManager、EventBus 或内部 Lane 锁。

### 5.4 Plugin 注册示例

```python
inject = ("compaction",)
provide = ()


def apply(ctx: Context, config=None):
    async def pressure(payload: AgentPreStep, next_):
        if payload.signal.aborted:
            return await next_()
        try:
            await ctx.compaction.compact_if_needed(payload.agent, payload.signal)
        except CompactionPolicyError as error:
            ctx.logger.warning("compaction pressure skipped: %s", error)
        return await next_()

    ctx.on(AGENT_PRE_STEP, pressure)
```

监听器注销由 Plugin Fiber 自动管理，不允许 Plugin 额外维护全局监听器表。

## 6. 开发计划

### F6.1 cordis-py 0.4.0 官方运行时接入

- 在当前环境安装 `E:\cordis-py` 的 editable distribution，确认版本为 0.4.0。
- 从 ftre 包树删除 `src/cordis`，更新 pyproject、CI/开发安装说明和 import origin 契约测试。
- 保持 ftre 只消费官方 Context/Fiber/Service/Inject/Effect/Events，不修改 `E:\cordis-py`。

### F6.2 官方 API 适配与重复内核删除

- 将 Plugin 入口注解从不存在的 `PluginContext` 改为官方 `Context`。
- 将 Loader/Manager/Diagnostics 从简化版 `plugin(id=...)`、`context.settle/unload`、`fiber.missing/error/provided` 迁移到官方 Registry/Fiber API。
- 删除 `_failed_fiber` 等伪造状态对象；导入/验证失败用 ftre 诊断记录表达。
- 用官方 Fiber await/dispose/restart 和 root Context dispose 验证可逆生命周期。

### F6.3 Hook 契约基线与架构门禁

- 冻结现有 Agent/Tool/Session/Command/Compaction 关键行为测试。
- 建立旧 Hook、直接私有依赖和跨 Feature import 清单。
- 先写禁止回流的 architecture tests。

### F6.4 类型化 HookSpec、作用域与诊断

- 基于官方 Events 五种模式实现 HookSpec、类型边界和领域契约。
- 实现 `once`、`prepend`、监听快照、重复 `next_()` 防护和诊断。
- 仅在官方 Fiber/Effect 之上补充 ftre Hook 作用域和 in-flight 追踪，不复制 Cordis 生命周期。

### F6.5 Agent Registry 与 AgentLoop Provider 分层

- 将 Agent 公共 Protocol、Registry、Inbox 与 Hook 契约从具体 Loop 中分离。
- 建立 `services/agent/contracts.py`、`services/agent/registry.py` 和 `services/agent_loop/` Provider。
- `AgentService` 只暴露显式 `AgentDriver` 端口、Agent identity、status 和 scope 查询，不保留 `loop` 属性、`_call()` 泛转发或具体 `AgentLoop` 类型。
- `AgentLoopProvider` 是唯一构造 `AgentLoop` 的 Owner，输出 `AgentLoopDriver`；Composition Root 可以持有 Provider runtime 做 Host 启停，但不得把具体 Loop 注入 AgentService。
- AgentRegistry 删除后重新注册同一字符串 id 必须生成新的运行时 identity；Hook scope 不得跨生命周期复用。

### F6.6 Agent 状态机语义 Hook 与 pending 领取治理

- 接入 Agent 生命周期和 Inbox 观察 Hook。
- 接入 pre-step/request/request-error/turn-stopping 控制 Hook。
- 调整为 peek → pre-step → claim/commit，覆盖取消、拒绝和并发新消息。
- 保持不同 Agent 并行、同一 Agent 单 active turn。

#### F6.6 实现边界与验收记录

本阶段已把四个控制 Hook 接入真实 Agent 数据面，而不是只增加契约文件：

| 语义边界 | 运行位置 | 控制结果 | 失败/取消语义 |
|---|---|---|---|
| `agent/pre-step` | `SessionLane._drain()` 的 `peek` 与 `MailboxStore.take` 之间 | `EnterStep`、`RejectStep(keep/discard)` | Hook 异常或取消不 claim；`keep` 保留队首并进入 blocked，`discard` 先完成持久 cancel 再发观察事件 |
| `agent/request` | `TurnExecutor._build/_build_resume` 创建 `ReActAgent` 前 | 只能返回新的 `AgentConfig` 快照 | 消息历史不通过该 Hook 暴露或修改；取消信号已置位时 Turn 进入取消路径 |
| `agent/request-error` | `TurnExecutor._run` 的错误结局 | `RetryRequest(reason, progress_token, max_attempts)` 或 `None` | 重试必须拥有未使用的 progress token 且未超上限；重复 token、取消或 Hook 异常均终止原错误 |
| `agent/turn-stopping` | `TurnExecutor.execute()` 的统一 `finally` 屏障 | serial、只观察 Turn 终态 | 正常、异常、取消均只触发一次；观察失败不会跳过基础收尾 |

Inbox 的状态转换由同一个 `SessionLane` 串行所有者保护：

```text
pending
  │
  ├─ peek（不改变持久状态）
  ├─ ContextGate（压缩时仍保持 pending）
  ├─ agent/pre-step
  │    ├─ RejectStep(keep)    → pending + blocked
  │    ├─ RejectStep(discard) → cancel_pending + discarded
  │    └─ EnterStep           → take/claim
  └─ TurnOperation（内存 active，完成后继续下一条 pending）
```

`agent/inbox/inserted`、`claimed`、`discarded`、`agent/session-start`、`status`、
`created`、`disposed` 和 `error` 作为观察型 Hook 只报告已经发生或正在发生的
状态坐标，不承担持久化和调度责任。每个候选/Turn 使用独立 `asyncio.Event` 取消
信号；pre-step 等待期间关闭 Lane 会先置位该信号再取消 worker，已 claim 的 Turn
则由同一个信号贯穿 request、request-error 和 turn-stopping。

对应测试覆盖：

- `tests/contracts/test_f6_agent_hooks.py`：pre-step 顺序、keep/discard、Inbox mutation
  观察、request 配置替换、request-error token 上限、turn-stopping 类型边界和取消信号；
- `tests/test_session_lane.py`：既有 FIFO、claim/取消、同 session 串行和 pending 恢复回归；
- `tests/architecture/test_f6_agent_layer.py`：AgentService/AgentLoop Provider 边界不回流。

本阶段不把 steer/next-step 输入重新塞入 mailbox；`turn-stopping` 先作为统一 serial
收尾屏障，后续若引入 steer API，必须通过公开 Agent Service 定义输入所有权和持久化
边界，另行更新本 PRD，避免 Hook 监听器直接修改 Turn 内部列表。

### F6.7 Tool、Session、System Prompt Hook 管线

- 落地 Tool 四阶段管线（pre/execute/post/result）；Owner Guard 作为后续安全切片。
- 落地 Session post-commit event 与唯一 flush 屏障。
- 用结构化 PromptAssembly 替代首个 system message 原地拼接。
- 增加 LLM around stream Hook。

#### F6.7 实现边界与验收记录

本阶段把 Agent Core 的执行接口收口到 `E:\\ftre\\src\\ftre\\infrastructure\\agent_core`，
不修改 `E:\\ftre-agent-core`：

| 管线 | 类型化契约 | 运行时 Owner | 结果语义 |
|---|---|---|---|
| Tool | `tools/pre-execute`、`tools/execute`、`tools/post-execute`、`tools/result` | `ToolHookBridge` + `HookedToolRegistry` | pre 可 Allow/Deny/替换参数；execute 是 around continuation；post 可替换结果；result 只观察最终结果 |
| Session | `session/event`、`session/flush` | `AgentLoop.emit_session_event`、`SessionService.flush()` | `session/event` 位于 `SessionProjection.apply()` 持久提交之后；flush 是唯一公开并行屏障 |
| Prompt | `system-prompt/assemble` | `SystemPromptService.assemble_result()` + `TurnExecutor` | sections 先渲染为冻结 `PromptAssembly`，Hook 只能替换 Assembly，不能原地修改消息历史 |
| LLM | `llm/stream` | `HookedLLMAdapter` | around Hook 包装 Core Provider async iterator，保持消息、工具、取消信号和流结束语义 |

Tool 的 Core 适配只存在于 `infrastructure/agent_core/tool_adapter.py`：每个 Turn 创建
独立 `ToolHookBridge`，使用 `ContextVar` 绑定 call identity，避免并发 Tool 调用串台；
`HookedToolRegistry` 只包装当前 Agent 的 Tool view，不改变全局 ToolService 注册表。
Core 的 `FtreCoreHookManager` 不再由产品 Plugin 或 Gateway 直接注册，只有该适配器
使用它承接 Core 的 pre/post callback。

Composition 创建一个共享 `hook_runtime` Service。Plugin 通过当前 Fiber 的
`HookRuntime.register(..., context=ctx)` 注册，AgentLoop、Session、Prompt、Tool 和 LLM
共用同一 Cordis event graph；Plugin unload 时由 Fiber Effect 自动注销。

旧 `agent/before_messages_build` / `agent/before_run` 在真实 Gateway `Context` 路径已不再
触发，session-title 已迁移到 `system-prompt/assemble`。当前仅保留一个针对非 Context
transition/test host 的显式 fallback；F6.9 已删除该 fallback 及旧契约文件，所有
消息构建扩展统一经过结构化 PromptAssembly。

测试覆盖位于 `tests/contracts/test_f7_hook_pipeline.py`，验证 Tool 参数/around/body/结果
顺序、Prompt Assembly waterfall、Session flush 唯一入口和 LLM async stream 包装；全量
回归同时覆盖既有 HITL、SessionProjection、Composition、Feature 和 Agent 生命周期。

### F6.8 Compaction Plugin Hook 化迁移

- 创建独立 Compaction Feature Owner。
- 自动压力接入 `agent/pre-step`。
- overflow recovery 接入 `agent/request-error`。
- 手动压缩接入 maintenance 和公开 Service。
- 删除 AgentLoop 对 CompactManager 的直接持有。

#### F6.8 实现边界与验收记录

压缩实现已从 `services/agent_loop/runtime/compaction` 迁入
`features/compaction/service.py`，公开端口收敛为 `services/compaction/contracts.py`
中的 `CompactionPort`。AgentLoop、ContextGate 和 Command 只依赖这个 Protocol/Service
端口，不 import Feature 私有实现，也不再持有 `CompactManager`。

| 入口 | Owner | 语义 |
|---|---|---|
| `agent/pre-step` | `features.compaction.plugin` | 使用本条 pending 的 config/channel/额外 token 做压力判断；失败 fail-open，取消继续遵守 Turn signal；成功后仍交回下游 `EnterStep` |
| `agent/request-error` | `features.compaction.plugin` | 只处理 overflow/context-length 类错误；压缩 generation 必须前进，才返回一次性 `RetryRequest`；无进展、取消或异常继续原错误 |
| `/compact` | Command → `loop.compaction.compact_now()` | 通过公开 Service 进入独占 maintenance，不进入 Inbox |
| `/compress-fast` | Command → `loop.compaction.compress_fast()` | 仍使用公开 Service，保持零 LLM 裁剪语义 |

`CompactionService` 保留原有 per-session shared Task 去重和 cancel/close 语义，新增
`progress_generation`：overflow recovery 不能仅凭异常捕获就重试，必须看到摘要或 fast
裁剪产生的可证明进展。Feature 通过共享 `hook_runtime` 注册 Hook，并由 Fiber Effect
负责卸载；Provider 在构造 AgentLoop 后绑定唯一 Session event emitter，压缩结果仍经
`SessionProjection` 投影，不直接写 state 或发送 WebSocket。

没有加载 Compaction Feature 时，AgentLoop 使用 `NullCompactionService`，队列和 Agent
仍可运行但不会自动压缩恢复；默认 Composition 会显式加载内置 Compaction Feature。

对应测试：`tests/contracts/test_f8_compaction_feature.py` 覆盖 Feature 注册、overflow
进展与 RetryRequest；`tests/architecture/test_f8_compaction_boundaries.py` 确认旧目录
删除、AgentLoop/ContextGate/Command 只使用公开端口；既有 `test_compact_*` 全部迁移到
Feature Owner 路径并保持行为回归。

### F6.9 Command 解耦与旧 Hook 删除

- Command 在 `AgentLoop._consume` 接入层解析。解析成功后：系统命令走 control
  lane；普通命令交给目标 `SessionLane.dispatch_command`，在同会话 admission lock
  后执行，绝不写入 `MailboxStore`。
- `CommandService.parse()` 是唯一解析入口，`dispatch_inbound()` 是唯一从 Bus
  信封派发入口；`TurnExecutor` 只接收普通消息或已解析的 `CommandResult`，不再
  读取 `CommandManager`、匹配文本或决定命令路由。
- `SessionLane.dispatch_command()` 等待此前已接纳的 pending drain 完成，再执行
  普通命令，阻止新消息越过命令；命令本身不占用 pending 容量。需要留痕的命令
  （例如 `/compact`）由 `TurnExecutor.execute_command()` 通过公开 SessionProjection
  写入一次 UserMsg，不将命令文本再次送入模型。
- `/compact` 与 `/compress-fast` 只通过公开 `CompactionPort`/`CompactionService`
  调用；Command 层不 import AgentLoop、TurnExecutor 或 Compaction 私有实现。
- 删除 `services/agent_loop/runtime/hooks.py` 以及 `before_run`、
  `before_messages_build`、`MessagesBuildContext`、`AgentRunContext` 和旧 Filter
  waterfall；System Prompt 统一使用 `system-prompt/assemble` 结构化 Hook。
- 新增 `tests/architecture/test_f9_command_boundaries.py` 与
  `tests/contracts/test_f9_command_ingress.py`，门禁接入层解析、命令不入 Inbox、
  旧 Hook 文件删除和 Command 私有依赖清零。

### F6.10 生命周期、作用域、并发与故障测试

- HookRuntime 的每个注册同时绑定 Cordis Fiber Effect；Fiber unload/restart 会标记
  监听器已注销并清理旧 registration。显式 dispose 与 Fiber 自动 dispose 都是幂等的，
  不会在诊断快照中累积可执行的旧 Listener。
- 控制型 Hook 有 in-flight quiescence 屏障：卸载先阻止新调用，再等待 active_calls
  归零；观察型 Hook 仍按失败策略隔离，不把用户 payload 写入诊断。
- 生命周期测试覆盖 Plugin reload/restart、in-flight listener、Agent 同 id 重建后的
  scope 隔离；作用域只按对象 identity 和父链命中，旧 Agent listener 不会命中新周期。
- 故障测试覆盖 pre-step Hook 异常后的 pending 保留与重试、Turn cancellation 后不接受
  RetryRequest、压缩失败后的 BLOCKED/显式取消恢复，以及 FIFO 请求只执行一次。
- 新增 `tests/lifecycle/test_f10_lifecycle_faults.py`，将“释放资源、等待 in-flight、
  保留 pending、禁止重复执行”作为可观察验收条件，而不是只验证状态枚举。

### F6.11 全量验收与执行报告

- 已完成对照 PRE/FR/AC 逐条验收，F6 核心范围标记为 `已验收`。
- 已运行全量 pytest、Hook/契约/架构/生命周期专项、ruff、YAML、diff check 与 Gateway smoke。
- 已同步 PRD、TODO、CHANGELOG 和
  `docs/execution/EXECUTION-F6-semantic-hook-system.md`。
- Git commit/push/merge 保持在用户明确授权后的交付步骤；F6.12 PyPI 发布仍是独立后置任务。

### F6.12 cordis-py PyPI 发行物切换与洁净安装验收（后置发布任务）

- 本任务不阻塞 F6.3-F6.11；在用户决定公开发布 `cordis-py` 前保持 `todo`。
- 等待 PRE11-PRE13 的 `cordis-py` 发行物和 PyPI 项目完成；不得在 ftre 内伪造或 vendoring 发行物。
- 在无 sibling checkout、无 `src/cordis` 的干净虚拟环境中完成 PRE14，确认 ftre 依赖解析到目标版本。
- 将 CI、开发安装说明和 import-origin 契约切换到 PyPI，执行全量测试、ruff、diff check 与 Gateway smoke。
- 将发行版本、包哈希、安装命令、CI 运行链接和失败回滚方式写入 F6 执行报告；未完成外部发布时不得把 F6 标为已验收。

## 7. 验收标准

- [x] AC1：Hook Runtime 单元测试证明 `emit` 会调用全部监听器，任一同步/异步观察者失败不会阻止后续观察者，并留下 owner/name 诊断。
- [x] AC2：`parallel` 测试证明所有监听器并发启动、全部 settled 后返回，多个失败以聚合异常呈现。
- [x] AC3：`serial`/`bail` 测试证明注册顺序、bail 值和无 bail 完整屏障符合 FR5-FR6。
- [x] AC4：`waterfall` 测试得到 `A before → B before → default → B after → A after`；短路不执行后续/default；同一 `next_()` 调用两次被拒绝。
- [x] AC5：Fiber unload/restart 测试证明监听器自动注销，无新调用进入已卸载 Plugin，in-flight 控制 Hook 收敛后无 Task 或 Effect 泄漏。
- [x] AC6：作用域测试覆盖 global、Agent A、Agent B、父 Agent、子 Agent和同 id 重建；只允许 global/同 scope/祖先监听器命中。
- [x] AC7：融合分发器测试证明 scope Agent 与 payload Agent 无法不一致，公共 Agent Hook 不允许 raw `Any` payload。
- [x] AC8：Agent Registry 生命周期测试证明 created/disposed 成对、观察者失败隔离、status 不产生无效重复迁移。
- [x] AC9：Inbox Hook 测试证明 inserted/claimed/discarded 与真实 mutation 一一对应，claimed 带准确 Turn，观察者失败不改变队列。
- [x] AC10：pre-step 测试证明压缩等待、Prompt Hook 失败和 Hook 取消期间消息仍为 pending；只有 EnterStep 后领取；RejectStep 的 keep/discard 语义无歧义。
- [x] AC11：并发测试证明同一 Agent 最多一个 active turn，不同 Agent 可并行；pre-step 运行期间新增 followup/steer 不被错误并入候选批次。
- [x] AC12：request Hook 测试证明可替换模型配置但不能修改消息；request-error 只有类型化 RetryRequest 且恢复产生持久进展时才重试。
- [x] AC13：turn-stopping 测试证明 serial 收尾屏障完整执行；F6 不暴露业务层 `steer()`，无 continuation 输入时 Turn 正常关闭。
- [x] AC14：Tool 管线测试证明执行顺序为 pre → guard → execute/body → post → finalize → result；Guard deny 不能被后续放行；result 观察者不能修改最终结果。
- [x] AC15：Tool 取消与卸载测试证明已启动 Tool 等待 quiescence，未启动调用得到稳定取消结果，不泄漏 Task。
- [x] AC16：Session 测试证明 `session/event` 在事实提交后通知且失败隔离；`SessionService.flush()` 调用全部持久化监听器并传播聚合失败。
- [x] AC17：System Prompt 测试证明 Skill、MCP、Plan、Title 等贡献通过结构化 assembly 工作，生产代码不再调用 `append_to_first_system`。
- [x] AC18：Compaction 测试证明 AgentLoop 不 import/持有 Compaction 私有对象；压力压缩由 pre-step Hook 触发，overflow 仅在持久进展后触发 retry。
- [x] AC19：maintenance 测试证明手动压缩与 Turn 不并发，压缩期间新消息可 pending，结束后自动唤醒；取消和失败均恢复稳定状态。
- [x] AC20：Command 测试证明 slash command 不进入 Inbox/模型消息；`/compact` 只调用公开 CompactionService；Command 不 import AgentLoop/TurnExecutor。
- [x] AC21：架构扫描确认生产代码不存在 `agent/before_messages_build`、`agent/before_run`、`MessagesBuildContext`、`AgentRunContext`、`append_to_first_system` 或旧 Hook Filter 调用。
- [x] AC22：架构扫描确认 Feature 不 import `services.agent_loop` 私有模块，Agent/AgentLoop 不 import `features.compaction`，Command 不 import Loop/TurnExecutor/Compaction 私有实现。
- [x] AC23：Composition startup 测试证明全部必选 Hook Provider 正常激活；缺失依赖保持 PENDING；Plugin unload/restart 后 Hook 数量、Service 和后台任务恢复基线。
- [x] AC24：现有 HTTP/WS、Session、Agent、Tool、Skill、MCP、Team、Schedule 行为回归通过，不需要客户端改动。
- [x] AC25：执行 `python -m pytest -q` 全部通过。
- [x] AC26：执行 `python -m ruff check --no-cache src tests` 全部通过。
- [x] AC27：执行 `git diff --check` 无错误，生产树不存在 `__pycache__`、`.pyc`、空迁移目录或无职责转发壳。
- [x] AC28：完成 Gateway 启动/关闭 smoke，关闭后无未等待 Hook、Tool、Agent driver 或 persistence Task。
- [x] AC29：完成 F6 总执行报告，逐条记录 FR/AC 结果、测试数量、关键架构差异、提交清单和未完成 TODO；未满足项不得标记已验收。
- [x] AC30：仓库边界检查确认 F6 的代码、测试和文档变更全部位于 `E:\ftre`；产品 Plugin 不直接 import/register `FtreCoreHookManager`；在不修改 `E:\ftre-agent-core` 的环境中全量验收通过。
- [ ] AC31：发行物验收记录包含 TestPyPI 安装结果、正式 PyPI 项目地址、版本号、wheel/sdist 哈希和 `twine check` 结果；已发布版本未被覆盖。
- [ ] AC32：在不存在 `E:\cordis-py` 和 `src/cordis` 的干净虚拟环境中，`pip install -e .` 成功解析 `cordis-py==<目标版本>`，import-origin 不落入 ftre 源码树，且全量 pytest、ruff 和 Gateway smoke 通过。

## 8. 测试计划

### 8.1 Hook Runtime 单元测试

- 五种 dispatch mode 的顺序、返回值、短路和异常。
- waterfall 嵌套顺序、默认函数、重复 next 防护。
- 监听器快照、注册/注销竞态、once/prepend。
- Fiber rollback、unload、restart 和 in-flight quiescence。

### 8.2 作用域测试

- global 与单 Agent scope。
- 父子 Agent 继承和兄弟隔离。
- scope/payload subject 不一致拒绝。
- Agent dispose 后同 id 新 Agent 不继承旧 identity。

### 8.3 Agent 状态机测试

- IDLE/RUNNING/MAINTENANCE 状态迁移。
- followup/steer/inject 的 next-turn/next-step 语义。
- pending peek、pre-step、claim、reject、cancel 和崩溃窗口。
- request 路由、request-error retry、turn-stopping continuation。

### 8.4 Tool 与持久化测试

- Allow/Deny/Ask、Owner Guard、around execute、post replace/block。
- Tool 并发、顺序提交、取消和最终冻结观察。
- session event post-commit、write-behind listener、flush barrier。
- checkpoint 失败时模型请求和 Tool 副作用 fail-closed。

### 8.5 Compaction 与 Command 测试

- 压力压缩、overflow 恢复、重试上限和无进展不重试。
- 手动 maintenance、pending 唤醒、取消和失败清理。
- slash command 绕过 Agent；命令事件结构化且不污染模型历史。

### 8.6 架构与全量验证

```powershell
python -m pytest -q tests/hooks tests/contracts tests/architecture tests/lifecycle
python -m pytest -q
python -m ruff check --no-cache src tests
git diff --check
```

另执行一次前台 Gateway 启动/关闭 smoke，并检查退出后无遗留监听器、Task、临时文件和缓存目录。

### 8.7 发行物与洁净安装验证

以下步骤在具备 `E:\cordis-py` 发布权限后执行；当前仅登记为 F6.12 验收计划：

1. 在 `E:\cordis-py` 执行 `python -m build` 和 `python -m twine check dist/*`，记录构建文件名与 SHA-256。
2. 上传 TestPyPI，在全新虚拟环境中执行 `pip install --index-url https://test.pypi.org/simple/ cordis-py==<version>`，验证官方 API 和生命周期测试。
3. 正式发布后创建不包含 sibling checkout 的 ftre CI 运行，执行 `pip install -e .[dev]`、全量 pytest、ruff 和 Gateway smoke。
4. 使用 `python -c "import cordis, importlib.metadata; print(cordis.__file__); print(importlib.metadata.version('cordis-py'))"` 保存 import-origin 与版本证据。

## 9. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Hook 过多导致控制流不可读 | 调试困难、Plugin 顺序耦合 | 只开放语义边界；HookSpec 和诊断输出完整监听链 |
| waterfall listener 忘记调用 `next_()` | 意外短路默认行为 | 类型化结果、测试、诊断记录 short-circuit owner |
| Plugin unload 与 in-flight Hook 竞态 | 已卸载代码继续访问资源 | listener registration 活跃计数与 Fiber quiescence |
| scope 只用字符串 id | 同 id 生命周期串扰 | 使用对象 identity，Registry 绑定精确生命周期 |
| pre-step 前移改变 pending 时序 | 现有 ACK/等待行为回归 | 先写 characterization tests，单 Lane 内完成 peek/decision/claim |
| 压缩 Hook fail-open 掩盖长期故障 | 上下文持续增长 | 结构化告警、诊断状态、overflow fail-safe 与重试上限 |
| Tool middleware 改写身份或取消信号 | 审计与安全失真 | 冻结 identity、融合原始取消信号、Owner Guard 单调拒绝 |
| 一次迁移面过大 | 难以定位回归 | 按 F6.1-F6.11 切片；每片保持全量测试绿；禁止长期双轨 |

## 10. 开放评审项

以下内容在 PRD 从“草稿”进入“评审”时必须定稿，但不会阻止当前立项：

1. `RejectStep(disposition="discard")` 是否必须同步写入结构化 SessionEvent，还是只写 Inbox mutation 事实。
2. 当前 Session 存储是否已有足够事务能力实现 claim 与用户消息边界的原子提交；若不足，本阶段最小保证为“pre-step 成功前不 claim”，完整 EventStore 原子批次另立阶段。
3. `llm/stream` 契约和 ftre adapter boundary 必须在 F6 完整落地；评审只需决定除 checkpoint/telemetry 外还迁移哪些首批消费者，不能降级为只写契约不接生产路径。
4. Hook payload 运行时校验在生产环境采用全量校验还是仅开发/测试严格校验。

评审结论必须追加到变更记录，并更新受影响 FR/AC；不得在开发中由实现者自行决定。

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始草案：新增五模式 Hook Runtime、类型化 HookSpec、Agent scope、AgentLoop Provider 分层及 Agent/Tool/Session/Prompt 语义 Hook；以 Compaction 和 Command 解耦作为首个端到端迁移 | 现有两个可变 Filter 无法表达 DSH 式 continuation、scope、失败策略和 Plugin 生命周期，Agent runtime 仍承担过多产品行为 |
| 2026-08-21 | 将官方 `cordis-py 0.4.0` 接入提升为 F6.1/F6.2 前置门禁：删除 `src/cordis`，Plugin 入口统一使用官方 `Context`，Loader 改用官方 Fiber/Registry API，并补充 CI/导入来源验证 | 发现此前 ftre 运行时实际命中 Agent 临时编写的简化同名包；在错误内核上继续开发 Hook 会造成第二套生命周期和事件语义 |
| 2026-08-21 | 增加 PRE11-PRE16 与 F6.12：明确 `E:\cordis-py` 本地 editable 仅是 Hook 开发基座，正式交付必须经过 TestPyPI、PyPI 发行物、ftre 清洁安装和 CI 脱离 sibling checkout 的验证；新增 AC31-AC32 | 用户决定暂缓发布 cordis-py，先推进语义 Hook；发行物切换保留为后置任务，不阻塞 F6.3-F6.11 |
| 2026-08-21 | 启动 F6.3：新增 `platform/hooks` 稳定名称、HookSpec、HookRuntime 与无敏感 payload 的失败诊断；新增 Hook 专项测试和旧 Hook 引用白名单门禁 | 先建立可独立验证的契约基线，防止后续 AgentLoop/Feature 迁移形成新旧 Hook 双轨和跨层回流 |
| 2026-08-21 | F6.3 收尾并启动 F6.4：补充 Agent/Tool/Session/Command/Compaction 行为基线清单；HookSpec 增加 scope/result 类型边界；Runtime 增加 once、prepend、监听快照、scope carrier、重复 `next_()` 防护和 owner/order/active-call 诊断；工具 Hook 名称统一为 `tools/*` | 将基线门禁与第一批类型化 Hook 语义固化，后续 Agent scope 和领域 Hook 迁移必须在此契约上演进 |
| 2026-08-21 | F6.4 验收：补充五种 Cordis 调度模式、waterfall 短路/重复 next、once/prepend、监听快照、并行聚合失败、父子 scope/同 id 重建隔离、结果类型和 in-flight quiescence 测试；拆分 `diagnostics.py` 与 `scope.py` | 完成类型化 Hook Runtime 的第一版可验证语义，为 F6.5 Agent Registry 与领域 Hook 接入提供稳定基座 |
| 2026-08-21 | 启动 F6.5：新增 `AgentDriver`/`AgentRegistryProtocol`、AgentRegistry 和独立 `services/agent_loop` Provider；将真实 Loop/Lane/Mailbox/Compaction runtime 从 `services/agent/runtime` 迁入 `services/agent_loop/runtime`；AgentService 删除 Loop 泛转发，Gateway 通过 Driver attach；删除旧 factory | 让 Agent Service 只拥有 Agent identity/公开端口，具体 AgentLoop 构造和数据面依赖集中到唯一 Provider，为后续 Agent 状态机 Hook 化建立真实分层 |
| 2026-08-21 | F6.5 验收：AgentService/AgentDriver 契约、Registry identity 生命周期、Provider 唯一构造 Owner、真实 runtime 目录迁移、Gateway attach/detach 和架构门禁全部通过；全量测试 353 passed | 完成 Agent 公共 Service 与 AgentLoop 数据面 Provider 的真实分层，进入 F6.6 状态机语义 Hook |
| 2026-08-21 | F6.6 验收：接入 `agent/pre-step`、`agent/request`、`agent/request-error`、`agent/turn-stopping`；SessionLane 固化 `peek → decision → claim`，补齐 keep/discard、取消 signal、Inbox mutation 与 Agent 生命周期观察；全量测试 360 passed，ruff 全绿 | 将 Hook 从契约推进到真实 Agent 状态机边界，保证 Hook 失败/取消不提前领取或丢失 pending |
| 2026-08-21 | F6.7 验收：接入 Tool pre/execute/post/result、Session post-commit/flush、结构化 PromptAssembly 和 LLM around stream；新增 Agent Core 单向适配器与共享 `hook_runtime` Service；全量测试 364 passed，ruff 全绿 | 将 Tool、Session、Prompt、LLM 扩展点从旧可变 Filter/Core Hook 收口到统一 Cordis 语义边界 |
| 2026-08-21 | F6.8 验收：Compaction 实现迁入 `features/compaction`，公开 `CompactionPort`，通过 `agent/pre-step` 做压力优化、通过 `agent/request-error` 做有进展 overflow recovery；Command 改走 `compaction` Service；删除 AgentLoop 对 CompactManager 的直接持有；专项与既有压缩测试通过 | 让压缩成为可独立卸载的 Feature Owner，AgentLoop 只依赖公开能力，不再拥有压缩算法和任务表 |
| 2026-08-21 | F6.9 实现：CommandService 增加 parse/dispatch_inbound 接入 API；AgentLoop 在 Bus 消费边界先解析命令，SessionLane 以 admission lock 串行执行普通命令；TurnExecutor 删除 CommandManager 匹配/派发，仅执行已解析结果；`/compact` 与 `/compress-fast` 保持公开 CompactionPort 调用；删除旧 runtime/hooks.py、旧 Filter fallback 与对应兼容测试，新增 F6.9 架构/契约门禁 | 消除命令文本进入 Inbox/模型的隐性路径，收紧 AgentLoop/TurnExecutor/旧 Hook 的职责边界，完成 FR39/FR41/FR42 与 AC20-AC22 |
| 2026-08-21 | F6.10 实现：HookRuntime registration 绑定 Cordis Fiber companion Effect，Fiber unload/restart 标记并清理旧 Listener；增加 active_calls drain barrier 等待 in-flight Hook 收敛；新增 lifecycle/scope/fault 测试覆盖同 id Agent 重建、pre-step 失败重试、取消后拒绝 retry、压缩失败保留 pending 与去重执行 | 仅移除 Listener 不能证明 Plugin 生命周期安全；需要将“不可再进入、等待进行中调用、资源可逆、pending 不丢失”转成可观察的测试契约 |
| 2026-08-21 | F6.11 最终验收：补齐 `session/created` / `session/disposed` Hook；公共 Hook README 增加 owner、模式、payload/result、失败策略、scope 与副作用边界；完成 FR1-FR46、AC1-AC30 重核，保留 PyPI PRE11-PRE16/AC31-AC32 为 F6.12 后置项；新增 F6 执行报告 | 将实现、测试、文档和验收证据三联动收口；业务层 `agent.steer()` 不属于当前数据面，明确后置而不伪造完成 |
| 2026-08-22 | F11 后续变更：`agent/after-turn` 加入 ftre Agent 状态机；主动压缩从 F6/F10 的内置 Service/Feature 描述迁入可选 `ftre-compaction` 发行物 | F11 将压缩策略与 AgentLoop/Lane 解耦；当前实现以 `PRD-F11-compaction-gate-hook.md` 为准，F6.12 PyPI 发行仍后置 |
