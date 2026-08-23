# 执行提示词 01：F14.1 终局目录、Owner 与依赖基线

你正在 `E:\ftre` 后端仓库执行 F14.1。这是实现任务，不是只给建议的代码审查。持续工作直到
本批停止条件全部满足；不要中途询问“是否继续”。遇到超出授权的跨仓库变更或无法安全隔离的
他人未提交修改时，保留现场并报告精确 blocker，不要猜测授权。

## 一、开工前强制检查

1. 完整阅读 `AGENTS.md`、`docs/COMMIT.md`、`docs/PROCESS.md`、`docs/TODO.yaml`、
   `docs/prd/PRD-F14-final-plugin-first-architecture.md`。
2. 确认 PRD 已是 `approved` 或 `开发中`，TODO F14 为 `in_progress`；若仍为草稿，不开工。
3. 检查 `git status --short`、当前分支和最近提交。只在
   `feature/F14-final-plugin-first-architecture` 工作。不得 reset、checkout 丢弃或提交不属于
   F14 的既有修改。
4. 读取已有 F12/F13 执行报告和 F14 执行报告；报告不存在时创建
   `docs/execution/EXECUTION-F14-final-plugin-first-architecture.md`。
5. 本任务只改 `E:\ftre`。禁止修改 Desktop、`E:\ftre-agent-core`、`E:\cordis-py`，禁止
   push、merge、release。

## 二、本批目标

建立一个可由后续六批持续验证的事实基线。不能只复制 PRD 表格，必须从实际代码、Manifest、
imports、tests 和生命周期实现反推证据。

### 1. Owner 清单

逐项列出并写入 F14 执行报告：

- 当前每个 Plugin id、entry、required/default_enabled、inject、provide；
- 每个 Service key 的唯一 Provider 和真实实现；
- 每个 HookSpec 的定义 Owner、发布者、监听者、模式、失败语义；
- 每个 Route、Tool、Command、Channel、Task、Watcher、Store、Exporter 的生命周期 Owner；
- `ftre-inbox`、`ftre-compaction` 的 Host 接入点和跨包 import；
- 所有 `bind_*`、动态 `ctx.get()`、Service Locator、全局 setter、Port、Facade、Coordinator、
  compatibility alias 和跨 Owner 私有 import。

每项结论必须附文件路径/符号证据，不能写“看起来”“应该”。

### 2. 迁移映射

建立“当前路径 → F14 目标路径 → 唯一 Owner → 所属批次”映射，至少覆盖：

- `src/ftre/kernel`；
- `src/ftre/services/agent_loop`；
- `src/ftre/features`；
- Command、Trace、Session Title；
- concrete WebSocket/Subagent Channel；
- `packages/ftre-inbox`、`packages/ftre-compaction`。

不得在本批提前大规模移动这些路径；本批负责把事实和门禁建立准确。

### 3. 架构测试基座

在 `tests/architecture/` 增补可复用的扫描/断言，守住迁移前后都成立的规则：

- Service key/Plugin id 不重复；
- Manifest entry 可以解析到唯一 Plugin Owner；
- Kernel 机制层不能依赖业务实现；
- Plugin/Service 不跨 Owner import Repository/Runtime 私有实现；
- 禁止全局 setter、Service Bag 和已退役兼容入口；
- Package 不通过 concrete import 反向侵入 Host。

测试不能使用 `skip`、`xfail`、空断言或过宽 allowlist 掩盖已知问题。现有债务若属于后续批次，
在执行报告列为带路径、Owner、清理批次的基线项；门禁必须确保债务数量不会增加。

## 三、中文注释与协作规则

- 新增扫描器、fixture、复杂断言时，用中文解释它保护的架构规则及误报边界。
- 不给显然的循环、赋值和 import 写逐行注释。
- 读取并保留现有注释表达的业务不变量；若注释与真实代码冲突，先以测试/调用链求证，再同步
  修正代码和注释，不得只删注释让矛盾消失。
- Owner 表和代码注释使用相同术语：Kernel、Service、Plugin、Hook、Package、Runtime。

## 四、验证与工程卫生

至少执行：

```powershell
python -m pytest -q tests/architecture tests/contracts tests/startup
python -m ruff check src tests packages
git diff --check
```

同时使用 `rg`/AST 扫描核对清单，不得只依赖手工阅读。检查本批新增的临时文件、缓存、空目录、
未使用 helper 和调试输出；只清理仓库内已验证路径，不执行宽泛递归删除。

## 五、文档、TODO 与提交

- 在 F14 执行报告记录命令、结果、Owner 基线和后续批次输入。
- 在 PRD 变更记录补充“F14.1 基线建立”，但不提前勾选后续 FR/AC。
- 只有本批验证全部通过才把 TODO `F14.1` 标为 `done`。
- commit 前重读 `docs/COMMIT.md`；按“测试基座 / 文档基线”职责分批提交。不得 push。

## 六、停止条件

- Owner 清单、Hook 清单、迁移映射和债务基线均有代码证据；
- 架构测试基座真实运行且专项测试、ruff、相关 diff check 通过；
- 没有修改后续批次的生产目录来伪装完成；
- F14.1 文档状态与证据一致；
- 本批修改已提交，工作树只允许保留执行前就存在且明确不属于 F14 的修改；
- 最终汇报提交 hash、测试结果、债务数量和第 02 批的精确输入。
