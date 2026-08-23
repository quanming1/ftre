# 执行提示词 03：F14.3 Agent Runtime 内聚与唯一 Agent Service

你正在 `E:\ftre` 执行 F14.3。目标不是给 `agent_loop` 再包一层，而是让 `AgentService` 成为
唯一公开 Agent 能力，把 Loop、Driver、TurnExecutor、CompletionRegistry 和 Core Adapter
降为同一 Owner 下的私有 Runtime。

## 一、开工检查

- 完整阅读强制文档、F14 PRD、F14 执行报告和本提示词。
- 确认 F14.1-F14.2 已完成，代码已经使用 `ftre.kernel`，分支和工作树状态正确。
- 重新阅读 `services/agent`、`services/agent_loop`、Composition、Inbox Plugin、Command、Tool、
  Session、Prompt、Trace 的真实调用链及相关测试。
- 不修改 `ftre-agent-core`；若发现 Core 必须改变，记录为外部 blocker，不复制 Core 代码。

## 二、目标结构与公开面

目标为：

```text
services/agent/
├─ models.py / contracts.py      # InboundMessage、TurnOutcome 等稳定公开输入输出
├─ hooks.py
├─ service.py                    # 唯一公开 agents Service
├─ plugin.py                     # 唯一 Agent Provider Plugin
├─ profiles/
└─ runtime/                      # Owner 私有实现
   ├─ driver.py
   ├─ turn_executor.py
   ├─ completion.py
   └─ core_adapter.py
```

完成后不得存在顶层 `services/agent_loop`、`agent_runtime` Service key、第二 Agent Provider、
`AgentRuntimeService` 生命周期壳或外部业务代码 import 私有 Runtime。

## 三、实施步骤

1. 先画出 Agent Plugin 的全部真实 inject，并解决装载顺序，不得靠启动后 `bind_*` 修补依赖。
2. 将 Loop/Driver/Executor/Completion 等实现移动到 `services/agent/runtime`，同步生产和测试引用。
3. Agent Provider Plugin 创建 AgentService 及其私有 Runtime、provide `agents`、注册 Effect 并负责
   start/stop；不得 provide 第二个 runtime key。
4. AgentService 公共方法只表达 Agent 身份、`run(InboundMessage)`、active 状态和取消；不得暴露
   `.loop`、`.executor`、`.sessions`、`.commands`、`.inbox` 等 Locator 字段。
5. 删除 Command parser、Inbox admission/claim、Compaction 策略和 WebSocket payload 认知；这些
   属于后续 Messaging/Plugin Owner。若当前分流必须暂存到第 04 批，放在 Messaging 明确入口，
   不能留在 private TurnExecutor。
6. 保持每个 Session 最多一个 active Turn、取消、after-turn、Session history、Tool/LLM stream、
   confirmation resume 和 agent scope dispose 语义。
7. 删除旧目录、旧 Manifest、旧 Provider、compat import 和只转发一次的 DTO/Facade。

## 四、注释与可理解性

- `AgentService` 顶部中文注释必须明确：只执行已交付输入，不拥有 pending/Command/Compaction。
- Runtime 的并发字典、reservation、取消信号和 completion 清理要解释不变量与 `finally` 顺序。
- 保留能解释 duplicate tool side effect、Session 删除竞态、Reply 收尾的注释，并更新旧 Loop 名称。
- 对删除的中间层不要保留“兼容”“临时”“未来迁移”注释。
- 注释必须与测试断言一致；不能用注释许诺代码没有保证的 exactly-once。

## 五、测试与架构清理

新增/迁移测试至少覆盖：

- 唯一 `agents` provide；无 `agent_runtime` key/manifest；
- `AgentService.run(InboundMessage)` 正常、失败、取消、重复 Session 并发保护；
- after-turn、session event、tool/LLM Hook 和 agent scope 生命周期；
- unload/restart 后旧 Driver/Task/Hook/Service 不残留；
- 生产代码没有 `services.agent_loop` import；
- 外部 Plugin 无法通过公共 API取得 Loop/Executor 私有实现。

执行专项、全量 pytest、ruff、diff check。使用 `rg`/AST 扫描旧符号、重复 Owner、bind 方法、
空目录、缓存和死代码；清理本批产生的所有残留。

## 六、停止条件

- 实际代码只有一个 Agent Service/Provider/Service key；
- `services/agent_loop` 和所有旧 import 已删除，无 alias；
- active Turn、取消和 Hook 行为有回归证据；
- F14 PRD/执行报告/TODO `F14.3` 与真实结果同步；
- 本批分批提交且工作树干净，不 push；
- 汇报 public API 前后对照、删除项、测试结果、提交和第 04 批待接管的 Messaging 职责。
