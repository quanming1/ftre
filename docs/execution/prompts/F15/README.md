# F15 Host Hook 收敛分批执行提示词

本目录把 [PRD-F15](../../../prd/PRD-F15-hook-surface-convergence.md) 第二版拆成七个必须
串行执行的 AI 实现任务。F15 只修改 `E:\ftre` Host 与仓内 `packages/`：Host Hook
从 22 个收敛为 10 个，全系统从 29 个收敛为 17 个；`ftre-agent-core` 的 7 个 Hook
在本阶段冻结不变。

## 使用前提

1. 用户已经评审 PRD，并把状态推进为 `approved` 或 `开发中`；草稿状态禁止执行生产代码。
2. 只在 `feature/F15-hook-surface-convergence` 工作。不得直接提交 `develop`/`master`。
3. 七批严格串行。每批开始必须读取前序提交和
   `docs/execution/EXECUTION-F15-hook-surface-convergence.md`。
4. 每批提示词授权完成本批代码、测试、文档和职责单一的 commit；不授权 push、PR、merge、
   release，也不授权修改 Desktop、Agent Core、Cordis 或用户配置。
5. 工作区若有执行前遗留修改，先记录文件、来源和 Owner；不得把它们混入 F15 commit，
   也不得 reset、checkout 或覆盖。

## 执行顺序

| 批次 | TODO | 目标 |
|---|---|---|
| 01 | F15.1 | 生成 29 个 Hook 的事实基线并建立 17 项目标门禁 |
| 02 | F15.2 | 收敛 HookRuntime awaited、Context、Effect 与 in-flight 生命周期 |
| 03 | F15.3 | Agent Host 与 Messaging Hook 改名、删除和运行链迁移 |
| 04 | F15.4 | Session、Inbox、WebSocket 的通知与顺序语义收敛 |
| 05 | F15.5 | Compaction、Inbox、Command、Session Title 消费者原子迁移 |
| 06 | F15.6-F15.7 | 故障/并发覆盖、Package 独立发行和 Core 冻结回归 |
| 07 | F15.8-F15.9 | 全盘清理、全量验收、报告与文档闭环 |

## 共同纪律

- 不以 alias、双发、兼容桥、no-op fallback 或第二 HookRuntime 掩盖未完成迁移。
- 不把普通 Service 调用、MessageBus 事实或 Trace 强行改成 Hook。
- 中文注释解释 Owner、时机、失败、取消、并发、顺序和清理原因；不逐行翻译代码。
- 测试禁止 `skip`、`xfail`、空断言、长 sleep 和无限扩大 allowlist。
- 每批都扫描死代码、重复 DTO/Effect、陈旧引用、缓存、临时文件、空目录和调试输出。
- 只有真实命令和测试证据才能勾选 FR/AC、更新 TODO；执行报告必须保留命令与结果。

## 最终交付

第 07 批结束时，F15 feature 分支应包含职责清晰的分批提交，工作树干净，ftre CI 所需
门禁全部在本地复现通过。最终 Agent 只汇报结果和仍需用户执行的 push/PR，不自行发布。

