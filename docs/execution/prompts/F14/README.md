# F14 分批执行提示词

本目录将 [PRD-F14](../../../prd/PRD-F14-final-plugin-first-architecture.md) 拆成七个必须
串行执行的 AI 任务。每份提示词都是自包含执行契约，不需要依赖聊天历史。

## 使用前提

1. 用户已经评审 F14，并将 PRD 状态按流程推进到 `approved`/`开发中`；草稿状态禁止开工。
2. 从最新 `develop` 创建 `feature/F14-final-plugin-first-architecture`。如果仓库存在其他阶段的
   未提交修改，先由原 Owner 收尾；执行 AI 不得擅自提交、移动、覆盖或回滚它们。
3. 七批严格按编号串行执行，不并行修改同一工作树。
4. 每批提示词授权在正确的 F14 feature 分支内完成该批代码、测试、文档和分批 commit；
   不授权 push、PR、merge、release 或修改其他仓库。
5. 每批开始先读取上一批写入的
   `docs/execution/EXECUTION-F14-final-plugin-first-architecture.md`，但仍以 PRD 为唯一需求依据。

## 执行顺序

| 批次 | 覆盖任务 | 目的 |
|---|---|---|
| 01 | F14.1 | 冻结 Owner、依赖和迁移基线，建立持续门禁 |
| 02 | F14.2 | `platform → kernel`，业务 HookSpec 回归各自 Owner |
| 03 | F14.3 | `agent_loop → agent/runtime`，只保留一个 Agent Service |
| 04 | F14.4 | Messaging Ingress 接管 Command/Inbox 分流 |
| 05 | F14.5 | `features → plugins/builtin`，concrete adapter/行为归位 |
| 06 | F14.6-F14.7 | Host Service 边界、独立 Package 和发行门禁 |
| 07 | F14.8-F14.10 | 生命周期、全盘清理、全量验收和最终报告 |

## 每批共同的完成标准

- 不以 re-export、alias、fallback、第二 Owner 或跳过测试维持表面兼容。
- 不留下“下一批再删”的同批旧路径；跨批债务必须在 PRD 明确属于后续批次。
- 迁移代码时同步迁移或重写有价值注释；删除与新 Owner 冲突的陈旧注释。
- 不写逐行翻译式注释。中文注释必须解释 Owner、边界、失败语义、并发、取消和清理原因。
- 不产生 `__pycache__`、`.pyc`、临时数据库、临时队列、build/dist、空目录或调试输出。
- 新行为/修复有测试；架构规则有自动化门禁；生命周期变化有 unload/restart 测试。
- 只有存在可复现证据时才更新 TODO 状态和勾选 AC。
- commit 前完整重读 `docs/COMMIT.md`，按仓库 hook 允许的 type/scope 分批提交。

## 最终交付

第 07 批完成后，F14 feature 分支应满足：

- 所有 F14 代码、测试和文档已按职责分批提交；
- 工作树干净；
- 未 push、未 merge、未发布；
- 执行报告列出每个 AC 的命令、结果、提交和未完成项；
- 用户可以据此审查后自行决定是否 push 并创建 PR。
