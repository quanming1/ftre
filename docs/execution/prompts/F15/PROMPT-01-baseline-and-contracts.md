# 执行提示词 01：F15.1 Hook 事实基线与目标门禁

你正在 `E:\ftre` 执行 F15.1。这是实现任务，不是只输出建议。持续工作到本批停止条件
全部满足，不要中途询问“是否继续”。

## 一、开工检查

1. 完整阅读 `AGENTS.md`、`docs/COMMIT.md`、`docs/PROCESS.md`、`docs/TODO.yaml`、
   `docs/prd/PRD-F15-hook-surface-convergence.md` 和本目录 `README.md`。
2. 确认 PRD 状态为 `approved`/`开发中`、TODO F15 为 `in_progress`，当前分支是
   `feature/F15-hook-surface-convergence`。若 PRD 仍为草稿，只报告 blocker，不改生产代码。
3. 检查 `git status --short`、最近提交和执行前遗留修改。禁止 reset、回滚或提交非 F15 改动。
4. 创建或续写 `docs/execution/EXECUTION-F15-hook-surface-convergence.md`。
5. 只改 `E:\ftre`；不得修改 `E:\ftre-agent-core`、Desktop、Cordis，不 push/merge/release。

## 二、从代码生成事实清单

使用 `rg`、Python AST 和必要的运行时 snapshot，逐项记录并附文件/符号证据：

- 29 个 HookSpec 的名称、Owner、定义位置、mode、scope、failure policy、payload/result；
- 每个 Hook 的所有真实发布点、生产 listener、注册顺序和零 listener 默认行为；
- 每个 listener 所属 Plugin/Fiber、Context、receipt、Effect、in-flight drain 和 dispose 路径；
- `EMIT` 中执行异步清理、权威状态推送、持久化屏障的现有危险用法；
- 只有定义无发布、只有发布无消费者、重复完成通知、重复 mutation 通知和歧义命名；
- Host、`ftre-inbox`、`ftre-compaction` 对 Core Hook 的 import 方式，证明 Core 7 项冻结边界。

不能手写一张与源码脱节的表。优先把 AST 扫描器或可复用 helper 放入
`tests/architecture/`，并用中文解释扫描规则和误报边界。

## 三、架构门禁

建立迁移前后都可运行的测试：

- 当前基线精确为 29 个唯一名称，重复 HookSpec/名称直接失败；
- F15 目标集合精确为 PRD 第 3.2 节 17 项（Host 10 + Core 7）；
- 每个 Spec 有发布点；已登记为本阶段删除的债务数量只能下降不能增加；
- 生产 listener 有唯一 Plugin/Fiber Owner，重复 disposer 可被扫描定位；
- Core 7 个 Hook 不允许在 F15 被改名、复制 DTO 或增加兼容 adapter；
- Host 目标 Hook 不允许继续新增 `request`、`event`、`status` 等裸歧义名称。

债务基线可以作为带路径和目标批次的显式数据，但不得用宽泛目录 allowlist 掩盖新问题。

## 四、验证、文档与提交

至少执行：

```powershell
python -m pytest -q tests/architecture tests/contracts
python -m ruff check --no-cache src tests packages
git diff --check
```

更新执行报告中的事实表、债务数量、迁移批次和命令结果；在 PRD 变更记录写入 F15.1 证据，
但不提前勾选后续 FR/AC。仅在全部通过后把 TODO F15.1 标为 `done`。

commit 前重读 `docs/COMMIT.md`，按“架构扫描器/基线文档”职责分批提交，不 push。

## 五、停止条件

- 29 项当前事实和 17 项目标均能由代码/测试复现；
- 每项债务有路径、Owner 和清理批次；
- 专项测试、ruff、diff check 通过；
- 没有修改生产 Hook 来伪装基线完成；
- F15.1 文档状态与证据一致，本批改动已提交；
- 汇报提交 hash、债务数量和第 02 批精确输入。

