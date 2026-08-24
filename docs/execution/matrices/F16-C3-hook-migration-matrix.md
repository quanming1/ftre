# F16 / C3 Hook 迁移矩阵与共同验收标准

> 这是两仓批次 00 的共同事实表。Core C3 拥有协议和算法调用点，ftre F16 拥有 Host、Plugin、
> Package 消费和跨仓验证。矩阵在批次 01 只读扫描后必须补充精确消费者路径；未扫描的项不得标记完成。

## 1. 目标清单

| 当前 Hook | 目标 | 处理 | 最终计数 |
|---|---|---|---:|
| `tools/pre-execute` | `tool/before` | 改名，保留允许/拒绝/改参 | 1 |
| `tools/execute` | — | 删除 around 协议，执行器回归 Core 私有实现 | 0 |
| `tools/post-execute` | `tool/after` | 改名，保留结果归一化与改写 | 1 |
| `tools/result` | — | 删除独立观察事件，观察并入 after/Host 日志 | 0 |
| `agent/turn-stopping` | `agent/stop-decision` | 改名，Stop/Continue 行为不变 | 1 |
| `llm/stream` | `llm/stream` | 保持 | 1 |
| `agent/before-reasoning` | `agent/before-reasoning` | 保持 | 1 |

Core 目标为 5 个；F15 全系统目标为 15 个。计数必须由 HookSpec、Composition 和 AST 扫描生成，
不得仅根据表格手工勾选。

## 2. 跨仓 Owner 矩阵

| 责任 | Agent Core C3 | ftre F16 | 禁止 |
|---|---|---|---|
| HookSpec、payload、结果类型 | 唯一 Owner | 不复制 DTO | Host 自建同名协议 |
| Tool 实际执行 continuation | Core 私有 `ToolHandler` | 不持有、不包围 | Host `tools/execute` 兼容层 |
| before/after 策略 | 提供默认和 dispatch 边界 | Plugin 监听并按 Host 生命周期注册 | 第二执行器/双 dispatch |
| Stop/Continue 算法 | Core 触发和解释结果 | Plugin 提供策略 | Host 复制 ReAct 停止机 |
| HookRuntime/Scope/Fiber | 不拥有 | Host/Cordis 唯一 Owner | Core 依赖 Cordis |
| Inbox/Compaction | 不知道 | Package 自己拥有 | Core 直接 import Package |
| 版本与 wheel | Core 发行候选 | 锁定并洁净安装 | 本地 `sys.path` 绕过版本 |

## 3. 必扫路径

### Core

- `src/ftre_agent_core/hooks.py`
- `src/ftre_agent_core/agent/runner/tool_handler.py`
- `src/ftre_agent_core/agent/runner/_execute_acting.py`
- `src/ftre_agent_core/agent/runner/react_runner.py`
- `tests/`
- `pyproject.toml`、`CHANGELOG.md`

### ftre

- `src/ftre/kernel/hooks/`
- `src/ftre/services/agent/`
- `src/ftre/services/tools/`
- `packages/ftre-inbox/`
- `packages/ftre-compaction/`
- `src/ftre/plugins/`
- `tests/architecture/`、`tests/contracts/`、`tests/hooks/`、`tests/lifecycle/`
- `pyproject.toml`、锁定文件、CI 和 Gateway smoke

## 4. 共同不变量

1. Tool 一次调用最多执行一次真实 Tool；before 拒绝时执行次数为零；after 不拥有执行 continuation。
2. Tool 失败、取消、malformed arguments 和 Hook 异常的可观察结果与 F15/C2 基线一致。
3. Stop Decision 只在自然停止边界触发；`ContinueTurn` 继续下一次 Reasoning，计数限制不改变。
4. Hook 监听器的注册、卸载、异常和 in-flight 行为由 Host Scope 管理；Core 不持有宿主全局状态。
5. 未安装可选 Package 时，Host 不因缺失包导入失败，也不通过空兼容实现掩盖缺失能力。
6. 旧名称不得通过 alias、字符串 fallback、动态 getattr、双 dispatch 或测试 allowlist 复活。

## 5. 共同 AC

- [ ] AC-C1：两仓活动 HookSpec/Composition/Package 消费总数为 15，Core 子集为 5，key 无重复。
- [ ] AC-C2：`rg`/AST 扫描证明旧五个活动名称无生产消费者、无公共导出、无动态兼容入口。
- [ ] AC-C3：Tool before → 私有执行 → after 顺序和单次执行通过 fake Tool 回归。
- [ ] AC-C4：Stop Decision Stop/Continue、续写 prompt、最大次数、取消和异常通过 fake Agent 回归。
- [ ] AC-C5：Host、Inbox、Compaction 无跨 Owner 私有 import、Core DTO 复制、Service Locator、Facade 或第二执行器。
- [ ] AC-C6：Core wheel 和两个 Package wheel 在新临时 venv 中可安装、可导入，依赖版本可复现。
- [ ] AC-C7：无可选 Package 的 Host 最小 Composition 可启停并完成最小 Agent Turn。
- [ ] AC-C8：两仓全量 pytest、ruff、diff check、wheel、洁净安装和 Gateway/E2E 通过。
- [ ] AC-C9：仓库无缓存、构建物、临时 venv、空目录、调试输出和已确认死代码；报告保留命令证据。

## 6. 评审门禁

批次 00 只完成文档。以下条件全部满足后才能把两份 PRD 从“草稿”推进到“评审/approved”：

- 用户确认目标名称 `tool/before`、`tool/after`、`agent/stop-decision`；
- 用户确认删除 `tools/execute`、`tools/result`，且不保留 alias/双协议；
- F15 的最终状态和 C3 依赖关系明确；
- 批次 01 的只读扫描范围和证据格式无歧义。

## 7. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-08-24 | 建立 F16/C3 共同迁移矩阵与 AC | 为两仓分批执行提供单一对照表 |
