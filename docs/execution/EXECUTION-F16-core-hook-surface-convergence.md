# F16 执行报告：Core Hook 面终局收敛与 Host 迁移

> 当前批次：06（两仓最终验收）
> 状态：已完成本地验收，生成物清理和文档收尾完成
> 配对 Core 阶段：`E:\ftre-agent-core` C3

## 1. 范围与边界

本报告记录 F16/C3 从批次 01 到批次 06 的实际执行证据。只修改了 `E:\ftre` 与配对
`E:\ftre-agent-core`；没有修改客户端、`E:\cordis-py`，没有 push、merge 或发布到 PyPI。

## 2. 迁移前事实基线

| 事实 | 证据 | 结论 |
|---|---|---|
| F15 冻结全系统 Hook | `docs/prd/PRD-F15-hook-surface-convergence.md`、F15 执行报告 | 当前公共面为 17 个，Core 7 个冻结 |
| Core Tool 面 | `E:\ftre-agent-core\src\ftre_agent_core\hooks.py`、`agent/runner/tool_handler.py` | 四段 Tool Hook，包含 around 与观察事件 |
| Core 停止面 | `E:\ftre-agent-core\src\ftre_agent_core\agent\runner\_execute_acting.py` | `agent/turn-stopping` 返回 StopTurn/ContinueTurn |
| Package 边界 | `packages/ftre-inbox`、`packages/ftre-compaction` | 已有独立 entry point 和可选生命周期，需迁移旧 Core 引用 |

## 3. 批次 00–06 交付物

- `docs/prd/PRD-F16-core-hook-surface-convergence.md`：F16 PRD（现已验收）。
- `docs/TODO.yaml`：F16 阶段（现已 `done / 已验收`），依赖 F15/C3。
- `docs/execution/matrices/F16-C3-hook-migration-matrix.md`：跨仓唯一迁移矩阵与共同 AC。
- 本报告：记录当前事实、边界和后续输入。

## 4. 后续批次输入（已完成）

1. 用户授权 17→15、旧名删除和无 alias 策略。
2. 批次 01 完成两仓实际消费者、行为、版本和构建边界基线。
3. Core C3 完成协议与发行候选，ftre F16.5 升级版本并迁移 Host/Package。
4. 批次 06 完成洁净安装、E2E、清理和最终文档验收。

## 5. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-08-24 | 建立 F16 批次 00 报告壳，等待 PRD 评审 |
| 2026-08-24 | 完成 F16.1–F16.6 与两仓最终验收 | Core/Host/Package Hook 面已收敛 |

## 6. refactor-cleanup-audit 审计记录

### 范围与基线

- 仓库：`E:\ftre`；分支：`feature/F16-core-hook-surface-convergence`。
- 配对仓库：`E:\ftre-agent-core`；本次不修改客户端、`E:\cordis-py` 或外部仓库。
- F15.9 的远程 CI carry-forward 已在报告第 7 节标明；F16 当前为 `done / 已验收`。

### Owner 与入口证据

| 检查项 | 证据 | 结论 |
|---|---|---|
| 唯一 Composition Root | `src/ftre/app/gateway/composition.py` | Host 组装入口唯一，bootstrap 只负责进程启停 |
| HookRuntime 生命周期 | `src/ftre/kernel/hooks/runtime.py`、`tests/lifecycle/` | 由 Composition/Fiber 管理，已有 unload/失败清理覆盖 |
| 可选 Package 边界 | `packages/ftre-inbox/`、`packages/ftre-compaction/`、`pyproject.toml` | 现有 entry point/extras 边界保留，F16 负责新 Core Hook 版本迁移 |
| Core 依赖方向 | C3 执行报告中的 Core import 扫描 | Core 不 import ftre、Cordis 或 Package |

### 迁移前旧实现与引用基线

只读搜索确认以下旧 Core Hook 仍有真实消费者：

- `src/ftre/services/tools/hooks.py`、`src/ftre/services/tools/__init__.py`；
- `src/ftre/services/agent/hooks.py`、`src/ftre/services/agent/__init__.py`；
- `tests/contracts/test_f7_hook_pipeline.py`、`tests/architecture/test_f15_hook_surface.py`；
- Core 的 `hooks.py`、`agent/runner/tool_handler.py`、`agent/runner/_execute_acting.py` 和对应测试。

这些引用是 F16.1/C3 批次 01 的迁移基线；消费者迁移和行为回归完成后，旧常量、Spec、DTO、
导出和活动文档中的活动引用均已删除。

### 生命周期、测试与工程卫生

- ftre：`python -m pytest -q` → **485 passed**；`python -m ruff check src tests packages --no-cache` → **通过**。
- Core：`python -m pytest -q` → **238 passed**；`python -m pytest -q src/tests` → **51 passed**；
  `python -m ruff check . --no-cache` → **通过**。
- 两仓 `git diff --check` → 通过。
- 测试后清理：两仓 `__pycache__`、`.pyc`、`.pytest_cache`、`.ruff_cache` 均为 **0**；
  ftre `.ftre-inbox` 为 **0**；Core 空 `.ftre/skills` 已移除。
- 清理前已解析绝对路径并排除 `.git`；未删除用户数据、数据库、锁文件或 Core `.ftre/mcp.json`。

### 本次明确清理项

- 删除 `E:\ftre-agent-core\src\tests\aaa.py`：模块级直接启动真实 LLM、无引用、未纳入当前主测试套件，属于危险调试残留。
- 未删除 `src/tests` 其余手工集成脚本：它们仍有可收集测试或用途证据，留给后续独立清理，不扩大本批范围。
- 二次审计修正 `Gateway startup facade`、`session persistence facade` 和调度器测试名中的过时层级称谓，统一为实际入口/Service 语义。
- 二次构建重新扫描四个 wheel：文件数为 `47/188/12/12`，无测试、缓存、字节码或旧 Hook 活动代码；报告已同步最终 SHA256。
- 最终静态复核删除测试生成的空运行目录 `E:\ftre\.ftre-inbox`；仓库内空目录和生成物均为零。

### 审计结论

批次 00 的文档与工程卫生审计通过；旧 Hook 债务已按 C3→F16 顺序迁移。

## 7. 批次 01–06 执行证据

### 批次 01：两仓基线

- Core 迁移前公共 Hook 为 7 个，ftre 全系统为 17 个。
- 旧 Tool/Stop 消费者已逐一定位到 Core HookSpec、ToolHandler、ExitExecutor、ftre Service
  重导出、Package 和 Contract/Architecture 测试；无隐藏动态 alias。
- 版本边界冻结为 Core `0.2.0`、ftre `0.3.0`、Inbox/Compaction `0.2.0`。

### 批次 02–03：Core 协议迁移

- Core Tool Hook 已收敛为 `tool/before` 和 `tool/after`。
- 删除 `tools/execute`、`tools/result`、对应 around/EMIT DTO、Spec、导出和调用点。
- `agent/turn-stopping` 已改为 `agent/stop-decision`；Host 与 Package 全部使用新公共契约。
- Tool Handler 回归为 `before → 私有 invoke → after`，fake Tool 断言真实执行次数始终为 1。

### 批次 04：发行候选

- Core wheel：`ftre_agent_core-0.2.0-py3-none-any.whl`，47 个文件；
  SHA256 `e9dccea258c5632389b2e1546c7056fa12447065436203083ed4bf0709ecd7d1`。
- ftre wheel：`ftre-0.3.0-py3-none-any.whl`，188 个文件；
  SHA256 `d4a1ccdb94577eff49c5d69fbf9ce84c2186ef0ba402ef28d9e1d2772bedf41f`。
- Inbox wheel：`ftre_inbox-0.2.0-py3-none-any.whl`，12 个文件；
  SHA256 `217a942ddbbc132d2137d6b6002806a07efad8b9ef8f21e92f07235b2f9ee31b`。
- Compaction wheel：`ftre_compaction-0.2.0-py3-none-any.whl`，12 个文件；
  SHA256 `3785dde08ecaf019b16619c85778d1fcaffa79956bce9a26b8fecedb8d2f14a1`。
- 四个 wheel 均无测试目录、缓存、字节码和旧 Hook 引用；Core `setuptools` 已限制不打包 `src/tests`。

### 批次 05：Host/Package 迁移

- ftre `services/tools`、`services/agent`、Package 和架构门禁已切换到新 Hook。
- Host extras 与 Package 依赖锁定到 Core `>=0.2.0,<0.3.0`。
- 未安装 Inbox 时，Composition 通过 `find_spec` 不登记幽灵候选，不产生 FAILED Plugin；
  安装后 Inbox/Compaction 通过 entry point 发现并可显式启用。
- CI Core checkout 已绑定 `v0.2.0`，与 Host 依赖边界一致。

### 批次 06：洁净安装与 E2E

- 无可选 Package 的临时 venv：Core/ftre/cordis 版本分别为 `0.2.0/0.3.0/0.4.0`，
  Composition 启停成功，Inbox 状态列表为空。
- 安装两个 Package 后：entry point 为 `inbox`、`compaction`；两者均 `ACTIVE`；
  `compaction` restart 返回 `True`，unload 返回 `True`，Inbox 保持可用。
- 无 Dispatcher 的 fake Agent Turn 成功产生 6 个事件并输出 `ok`。
- ftre Package 独立测试：**60 passed**；架构/契约/启动/生命周期测试：**168 passed**；
  全量 ftre：**485 passed**；Core 全量：**238 passed**。
- 两仓 Ruff、wheel 内容扫描、`git diff --check` 均通过。
- Gateway smoke：临时 venv 中后台启动端口 `48661`，`GET /api/health` 返回 `200 {"status":"ok"}`，
  WebSocket 成功建立/关闭，`gateway stop --timeout 20` 返回 `[OK]`，最终状态 `not_started`。

### 当前收尾门

- F15.9 的远程 GitHub Actions 尚未触发，因为按仓库规则未 push；本地等价测试和洁净安装已完成。
- 本地工作树只保留本次 F16/C3 修改，未创建提交；用户未明确授权 commit/push。

### F17 后续审计输入

F16 验收后审计发现：`TurnExecutor._inbox` 从未由 Agent Provider 接线，
`send_message`/`task`/Team 的队列 Tool 仍由通用基础工厂注册。该问题不回写为 F16 的伪完成，
已转入 [PRD-F17](../prd/PRD-F17-inbox-tool-owner-convergence.md)，先完成 Inbox 基础队列
Owner 和 Agent Runtime 死透传清理；业务 Tool 的职责拆分随后由 [PRD-F18](../prd/PRD-F18-tool-package-boundaries.md)
完成。F16 的无 Inbox 洁净启动结果保留为历史快照，F17/F18 将当前 Gateway 运行时门禁调整
为必须有 Inbox，同时保持业务 Tool Package 可选。
