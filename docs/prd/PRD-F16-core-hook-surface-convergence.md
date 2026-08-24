# PRD-F16-Core Hook 面终局收敛与 Host 迁移

> 本阶段与 `E:\ftre-agent-core\docs\prd\PRD-C3-hook-surface-convergence.md` 配对。
> 这是 F15 之后的破坏性协议收敛阶段：先完成 Core C3，再升级 ftre Host 和仓内 Package。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F16 |
| 名称 | Core Hook 面终局收敛与 Host 迁移 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | 2026-08-24 |
| 关联文档 | `docs/TODO.yaml` F16；`docs/prd/PRD-F15-hook-surface-convergence.md`；配对 Core C3 |

## 1. 背景与目标

### 1.1 背景

F15 已将 ftre Host 的消费面、生命周期和 Package 边界收敛，并冻结全系统 17 个公共 Hook。
但 Core 仍暴露四段 Tool Hook 和一个语义不清的 `agent/turn-stopping`。这会让 Host、
`ftre-inbox`、`ftre-compaction` 及内置 Plugin 继续承受不必要的注册点、DTO 和版本耦合。

### 1.2 目标

配合 Core C3 完成以下终局迁移：

```text
F15 冻结的全系统 17 个
  - tools/execute
  - tools/result
  - tools/pre-execute → tool/before
  - tools/post-execute → tool/after
  - agent/turn-stopping → agent/stop-decision
  = 新的全系统 15 个稳定 Hook
```

Host 只消费 Core 的 5 个稳定 Hook；同一行为不通过旧名、别名、桥接适配器或第二次 dispatch
重复执行。没有 `ftre-inbox` 或 `ftre-compaction` 时，Host 仍能导入、启动和运行最小 Agent Turn。

### 1.3 非目标

- 不重新设计 Message/Inbox wire、Session 数据模型、客户端协议或 Agent 队列。
- 不把 Core 的 DTO 复制到 Host；不增加 `Port`、Service Locator、Facade、Coordinator 或 Service Bag。
- 不在 F16 迁移 MCP、Skill、Schedule、Team 为独立 Package；仅验证它们不受旧 Hook 影响。
- 不修改 `E:\ftre-agent-core` 的生产代码；Core 改动全部属于配对 C3。
- 不在本阶段直接发布 PyPI、push、merge 或创建 GitHub PR，除非另行授权。

## 2. 当前与目标公共面

| 当前 Core Hook | 目标 Hook | Host 迁移动作 | 删除原因 |
|---|---|---|---|
| `tools/pre-execute` | `tool/before` | 更新 HookSpec、监听器和测试 | 统一单数领域命名 |
| `tools/execute` | 无 | 删除所有消费和导出 | around 层仅包装 Core 私有执行器 |
| `tools/post-execute` | `tool/after` | 更新结果改写/脱敏/审计监听器 | 统一单数领域命名 |
| `tools/result` | 无 | 把必要观察移到 `tool/after` 或 Host 日志 | 独立 EMIT 不改变结果且制造重复事件 |
| `agent/turn-stopping` | `agent/stop-decision` | 更新 continuation/停止策略 | 明确这是决策而非“正在停止”事件 |

F15 已冻结且继续保留的 Hook：`llm/stream`、`agent/before-reasoning` 及其余 Host 稳定面。
最终清单必须由 Composition、Core HookSpec 和 AST 扫描共同生成，不能凭文档手工计数。

## 3. 功能需求

- [x] FR1：F16 只在配对 C3 进入 `approved` 且 F15 具备可复现基线后进入开发。
- [x] FR2：Host 只注册 `tool/before`、`tool/after`、`agent/stop-decision`，不再引用 Core 旧名称。
- [x] FR3：Tool 策略顺序固定为 before → Core 私有执行 → after；Host 不拥有原始 Tool continuation。
- [x] FR4：原 `tools/result` 监听器逐项迁移到 `tool/after` 或 Host 自有诊断，不复制通知、不重复审计。
- [x] FR5：Compaction、Inbox、Session Title、Command、Trace 和安全策略等现有消费者在新名称下行为等价。
- [x] FR6：`StopTurn`/`ContinueTurn` 的 continuation prompt、最大次数、失败和取消语义不变。
- [x] FR7：`ftre-inbox` 与 `ftre-compaction` 的 entry point、依赖和卸载语义继续独立；未安装时无导入错误。
- [x] FR8：删除 Host 中旧常量、旧字符串、compat alias、双 dispatch、死 Listener、无效测试和历史说明中的活动引用。
- [x] FR9：依赖版本明确指向 C3 发行候选；Host 不通过本地 `sys.path`、concrete import 或源码复制绕过版本边界。
- [x] FR10：增加架构扫描和回归测试，确保 Hook 数量、唯一 key、Owner、顺序和缺失可选 Package 门禁不会回退。
- [x] FR11：完成两仓全量测试、ruff、diff check、wheel、洁净安装、最小启动和 Gateway/E2E。
- [x] FR12：变更记录说明删除项和破坏性迁移；没有未经授权的跨仓提交、推送、合并和发布。

## 4. Owner 与依赖边界

| Owner | 允许负责 | 不允许负责 |
|---|---|---|
| Agent Core C3 | HookSpec、Core DTO、Tool/Stop 调用点、默认行为和类型校验 | Host 状态、Session、Inbox、Compaction、Cordis |
| ftre Host | 依赖版本、Composition、HookRuntime 注入、Builtin Plugin 生命周期、可选能力降级 | 复制 Core DTO、拥有原始 Tool continuation、重做 Core 算法 |
| `ftre-inbox` | Inbox Hook 的发布/消费和队列行为 | Core Tool Hook 兼容层、Agent 执行器 |
| `ftre-compaction` | Compaction Service/Hook 监听和压缩策略 | Core Hook 注册表、队列 claim、客户端通知 |
| 其他 Plugin | 通过稳定 Hook/Service 注入行为 | 直接 import 其他 Owner 私有实现 |

## 5. 跨仓迁移矩阵

完整矩阵和共同 AC 见 `docs/execution/matrices/F16-C3-hook-migration-matrix.md`。本阶段至少核对：

| ftre 区域 | 迁移动作 | 禁止结果 |
|---|---|---|
| `src/ftre/kernel/hooks/` | 只保留新 Core Hook key 的 Host 适配和诊断 | 旧 key alias、字符串 fallback |
| `src/ftre/services/agent/` | 更新 stop decision 消费和 continuation 测试 | AgentService 直接持有队列/Compaction |
| `src/ftre/services/tools/` | 将 Tool 策略迁到 before/after | 第二套 Tool 执行器 |
| `packages/ftre-inbox/` | 验证无旧 Tool Hook import，保持可选 | 强依赖 Host 私有模块 |
| `packages/ftre-compaction/` | 将溢出/压缩监听迁到新 Hook，保持可卸载 | 无包时 no-op 伪装或导入崩溃 |
| `tests/architecture`, `tests/contracts`, `tests/lifecycle` | 增加数量、旧名、Owner 和缺包门禁 | skip/xfail/过宽 allowlist |

## 6. 版本与兼容策略

F16 接受 C3 的破坏性公共协议变更：Host 必须升级 Core 版本后再运行。旧名不提供 alias，
也不保留“新旧同时 dispatch”过渡层。跨仓洁净安装必须验证锁定版本与 wheel 内容一致；失败时
应得到明确依赖/契约错误。

## 7. 验收标准

- [x] AC1：从 Composition、Core HookSpec、Host/Package AST 扫描生成的公共 Hook 恰好为 15 个。
- [x] AC2：Host、Inbox、Compaction、Builtin Plugin、测试和活动文档不含旧 Core Hook 的有效消费引用。
- [x] AC3：Tool before/after 顺序、拒绝、改参、结果改写、失败、取消和审计行为与 F15 基线等价。
- [x] AC4：Stop Decision 的 Stop/Continue、continuation prompt 和计数限制与 C2 基线等价。
- [x] AC5：未安装 `ftre-inbox`/`ftre-compaction` 时，Host 可 import、最小 Composition 可启停、最小 Agent Turn 可运行。
- [x] AC6：安装新 wheel 后，两个 Package 的 discovery、load/unload/restart、配置、数据目录和 Hook 消费通过。
- [x] AC7：没有跨 Owner 私有 import、Core DTO 复制、旧 alias、双 dispatch、全局 setter、Service Locator 或空兼容壳。
- [x] AC8：两仓 `pytest`、`ruff check`、`git diff --check`、wheel build、临时 venv 洁净安装通过。
- [x] AC9：真实 Gateway/WebSocket smoke 和最小 E2E 通过，用户消息、Tool 结果、停止/继续事件无丢失/重复。
- [x] AC10：缓存、构建物、临时 venv、空目录、调试输出和死代码清零；执行报告包含命令、结果、提交和剩余债务。

## 8. 分批计划

| 批次 | 仓库 | 交付 |
|---|---|---|
| 00 | 两仓文档 | PRD、TODO、迁移矩阵、共同 AC；等待用户评审 |
| 01 | 两仓只读 + 测试基线 | 冻结消费者、行为、版本和无迁移边界 |
| 02 | Agent Core | `tool/before`、`tool/after`，删除四段旧协议 |
| 03 | Agent Core | `agent/stop-decision`，删除 `turn-stopping` |
| 04 | Agent Core | 全量验证、wheel、版本和 Core 收尾 |
| 05 | ftre | 升级 Core、迁移 Host/Package/测试，17→15 |
| 06 | 两仓 | 洁净安装、E2E、清理、最终 PRD/TODO/报告验收 |

## 9. 测试与工程卫生

- 基线测试必须记录旧/新 Hook 清单、消费者数量和行为快照；不以 allowlist 掩盖已知债务。
- 迁移测试覆盖正常、拒绝、异常、取消、in-flight、卸载、重启、缺包和 Gateway 关闭。
- 每批使用 `rg`/AST 检查旧名、重复 Owner、动态兼容、缓存、build/dist、egg-info 和空目录。
- 中文注释只解释 Hook 时机、结果改写范围、失败/取消策略和为什么删除中间层，不逐行注释显然代码。

## 10. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | 建立 F16 草案，定义与 Core C3 的配对边界、17→15 目标和六批执行计划 | F15 冻结 Host 面后，继续清理 Core 协议冗余 |
| 2026-08-24 | 用户授权继续执行，PRD 进入开发中 | 批次 00 评审门已通过，按 01→06 顺序落地 |
| 2026-08-24 | 完成 F16.1–F16.6 本地实现、洁净 venv、wheel、Package 生命周期与 Gateway smoke | Core C3 与 Host/Package 已按迁移矩阵收敛 |
| 2026-08-24 | F17 后续修正：Inbox Tool Owner 迁移与 Agent Runtime 死透传清理另立 PRD；F16 的无包启动作为历史验收快照保留 | 审计发现 `TurnExecutor._inbox` 未由 Agent Provider 接线，队列 Tool 仍由通用基础工厂注册；不在 F16 已验收范围内静默扩大变更 | 转入 F17 |
