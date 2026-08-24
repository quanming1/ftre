# 执行提示词 03：F15.3 Host Agent 与 Messaging Hook 收敛

你正在 `E:\ftre` 执行 F15.3。读取强制文档、F15 PRD、前两批提交和执行报告，确认新的
HookRuntime API 已稳定。只改 Host Agent/Messaging 及直接测试；不要提前迁移 Package 消费者。

## 一、Agent Host 目标

把 ftre Agent 自有 10 个 Hook 收敛为 3 个：

- `agent/before-turn` → `agent/before-run`；
- `agent/after-turn` → `agent/after-run`；
- `agent/request-error` → `agent/run-error`；
- 删除 `agent/request`、`agent/turn-stopped`、`agent/created`、`agent/disposed`、
  `agent/error`、`agent/session-start`、`agent/status` 的 Spec、DTO、默认函数、dispatch、导出和测试。

逐个求证删除项：没有 listener 的发布点直接删除；只有定义无发布点的幽灵 Hook 彻底清零；
Agent scope 创建/销毁保持 AgentService 私有生命周期，不另造公共通知。

确保：

- before-run 在一条 `InboundMessage` 真正进入 Agent 执行前发布，拒绝时不调用 Core；
- after-run 在成功、失败、取消的统一终态恰好一次，并使用 awaited SERIAL 维护语义；
- run-error 只在可恢复错误路径运行，原错误、progress token、重试上限和取消语义明确；
- Agent Runtime 不识别 Compaction/Inbox 实现，也不持有 Package Service。

## 二、Messaging 目标

- `messaging/inbound` 原子改名为 `messaging/route`；它是 WATERFALL 控制路由，不是观察事件。
- 保持 Command-first、Inbox-second 的注册顺序；无人处理返回稳定 capability error。
- 不改变 Bus envelope、request_id、ACK、Desktop wire 或 Channel 协议。
- 本批可为后续消费者提供目标 Spec，但不得旧名与新名双发。

## 三、测试与卫生

新增/调整测试覆盖：before-run 拒绝、正常执行、错误、取消、after-run exactly-once、run-error
重试/拒绝重试、Messaging 路由顺序、无人处理和 listener 异常。架构扫描应证明删除的 Agent 名称
和 `messaging/inbound` 在生产源码不再出现，同时 Core 7 个 Hook 仍原样存在。

中文注释必须区分 Run、Reasoning、Tool Call；删除把 `request`/`turn` 混用的陈旧注释。扫描
compatibility alias、重复 DTO、无用 import、死分支和缓存。

至少执行：

```powershell
python -m pytest -q tests/contracts tests/architecture tests/hooks
python -m pytest -q tests/test_turn_lifecycle.py tests/test_gateway_runtime.py
python -m ruff check --no-cache src tests packages
git diff --check
```

更新执行报告/PRD/TODO F15.3，只按证据勾选。按 Agent/Messaging/测试职责提交，不 push。停止时
汇报目标发布点、删除清单、测试结果和仍待第 04/05 批迁移的消费者。

