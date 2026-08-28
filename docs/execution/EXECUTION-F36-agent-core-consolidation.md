# F36 Agent Core 合并执行报告

日期：2026-08-28
分支：`feature/F36-agent-core-consolidation`
配对仓库：`E:\ftre-agent-core`（C8）、`E:\binn\ftre-desktop`（事件协议清理）

## 1. 交付结论

F36.1–F36.8 已完成。Agent 公共契约、ReAct Runtime、LLM 协议和工具执行分别由
`ftre-agent`、`ftre-agent-runtime`、`ftre-llm` 和 Host `ToolService` 唯一拥有。
本次收口修复进一步将 Profile、SOUL/USER 和运行环境事实的最终组装统一到
`SystemPromptService`；Runtime 不再保留第二个 Prompt 组装入口。
Ftre 与 Desktop 的生产代码/活动测试不再导入 `ftre_agent_core`；Core C8 删除旧生产
包、旧测试、示例和发行元数据，不提供兼容 alias、re-export 或 `sys.modules` 映射。

本次只使用独立测试进程、fake provider、wheel 构建和静态扫描验证；没有 kill、restart
或迁移运行中的 Gateway。

## 2. Owner 与生命周期证据

| 对象 | 创建者 | 事实 Owner | 销毁/结束 |
|---|---|---|---|
| `AgentService` | `ftre-agent` Provider | identity、公开快照、准入 | `close()`，Runtime Provider 逆序释放 |
| `AgentLoop` / `TurnExecutor` | `ftre-agent-runtime` Provider | active Turn、Reasoning→Acting→Exit、Retry、Cancel、Confirm | Turn 终态后释放；维护屏障由 Runtime 追踪 |
| `LlmService` / Adapter | `ftre-llm` + Host LLM Provider | 请求、协议适配、StreamChunk、错误归一化 | Provider unload 撤销 Adapter 路由 |
| `ToolService` / `ToolView` | Host tools Provider / Tool Plugins | 注册贡献、作用域、权限、审批、执行、归一化 | contribution disposer；View 为一次性快照 |
| Agent event | `ftre-agent` | 回复/工具/思考/Retry/确认事件 | Session Projection 持久化或流结束 |
| Host event | `SessionEventService` | `PIPELINE_EVENT`、`SESSION_MAINTENANCE` | Host Projection/客户端消费；不进入 Agent 回复流 |

## 3. 阶段与证据

### F36.1–F36.4：边界冻结与契约迁移

- `ftre-agent` 持有 Msg、ContentBlock、AgentStreamEvent、ToolDefinition、Permission
  值模型和 Hook 契约；没有 Host/Core 导入。
- `ToolService` 只保存内部 contribution 索引，`prepare_view()` 返回不可变 ToolView；
  Runtime 通过 `tool_calls.py` 调度，不持有 Registry、MCP Client 或 callable。
- `ftre-llm` 是 StreamChunk、BlockAssembler、OpenAI Completions/Responses Adapter 和
  wire/error 归一化的唯一 Owner；Runtime、Compaction、Title 通过 LlmService 调用。

### F36.5：Runtime 唯一状态机

- `packages/ftre-agent-runtime/src/ftre_agent_runtime/react_runner.py` 合并
  Reasoning→Acting→Exit；`executors/reasoning.py` 持有一次 LLM attempt/retry，
  `executors/acting.py` 只消费 ToolView，`tool_calls.py` 负责并发/顺序/取消调度。
- AgentService `run()`/`stream()` 共用 Runtime control/admission；stream 输出真实带
  `run_id`/`sequence` 的 AgentStreamEnvelope。

### F36.6：事件协议

- SessionEventService 新增类型化 `HostPipelineEvent` 和 `SessionMaintenanceRecord`，
  通过 `session_event` topic 发送；Agent 回复仍走 `agent_event`。
- 删除无生产者的 DataBlock、ToolResultDataDelta、ExceedMaxItersEvent 和 CustomEvent
  reducer 分支；Desktop `chatProjection`、`chat`、Inspector 和 WebSocket 类型同步更新。

### F36.7：Core 依赖与发行退休

精确删除范围（均已确认不含 `work/`、配置、Session 数据库和 `.git`）：

- `E:\ftre-agent-core\src\ftre_agent_core\`
- `E:\ftre-agent-core\tests\`、`src\tests\`
- `message_to_openai_demo.py`、`resume_tool_call_demo.py`、`temp_hook_test.py`
- `pyproject.toml`（Core wheel 发布入口）
- `src/ftre/services/tools/filtering.py` 中旧的 `filter_tools(registry, ...)` 入口及其仅验证旧 Registry 的测试；allow/deny 现由 `ToolService.prepare_view()` 唯一执行，`coerce_tool_name_list` 作为配置形状校验保留。

保留：Core `docs/`、`AGENTS.md`、`CHANGELOG.md`（新增退休记录）、README 退休说明、
`work/` 和用户数据。

## 4. 验证命令与结果

### Ftre

```text
python -m pytest -q
711 passed in 266.98s (0:04:26)

python -m ruff check --fix --no-cache packages/ftre-agent packages/ftre-agent-runtime
  packages/ftre-llm packages/ftre-compaction packages/ftre-inbox packages/ftre-llm-recovery
  packages/ftre-llm-fallback packages/ftre-messaging packages/ftre-task packages/ftre-team
  src/ftre tests
Found 17 errors (17 fixed, 0 remaining).

rg -n "ftre_agent_core|ftre-agent-core|AGENT_CORE_ROOT" src packages tests pyproject.toml .github scripts
无输出
```

### Desktop

```text
node --check scripts/bundle-backend.js
通过

pnpm test
Renderer: 55 files / 537 tests passed
Platform: 12 tests passed

pnpm --filter @ftre/renderer exec tsc --noEmit
通过

git diff --check
通过
```

### Wheel 与洁净安装

构建 11 个 wheel：`ftre`、`ftre-agent`、`ftre-agent-runtime`、`ftre-llm`、`ftre-inbox`、
`ftre-compaction`、`ftre-llm-fallback`、`ftre-llm-recovery`、`ftre-messaging`、`ftre-task`、
`ftre-team`。逐一检查 `METADATA`，`core_dependency=False`。

在临时 target 目录使用 `pip install --no-deps` 安装 11 个 wheel：全部包可被发现，
`ftre_agent_core` 为 `None`。验证后删除临时 target 和 F36 构建目录。

### 跨仓残留扫描

- Ftre、Desktop 的生产代码、活动测试、CI、bundle 脚本：无 `ftre_agent_core`、
  `AGENT_CORE_ROOT`。
- 架构门禁测试中保留少量被测禁用符号的字面量，用于断言旧入口不存在；它们不构成
  import、运行时调用或兼容导出。
- Runtime/Host：无 `ToolRegistry(`、`ToolService.registry`、Core Adapter 工厂调用。
- ToolService：无 `filter_tools` 旧 Registry 入口；allow/deny 专项由 `prepare_view()` 测试覆盖。
- Core：旧生产目录、旧测试和发布入口已物理删除；`work/` 保留。

### 收尾审计补充

- 结构化 AST 扫描确认 Ftre `src/` 与 `packages/` 没有导入 `ftre_agent_core`；
  `AgentService`、`AgentLoop`、`ReActRunner`、`LlmService`、`ToolService` 各只有一个生产 Owner。
- 最终旧符号扫描无输出；Ftre 中 `src/ftre_agent_core` 与根 `pyproject.toml` 均不存在。
- 收尾复跑 Ruff（上述 Ftre 包、`src/ftre`、`tests` 范围，`--no-cache`）结果为
  `All checks passed!`；Ftre 与 Core 的 `git diff --check` 均无输出。Desktop 当前
  工作区的 `git diff --check` 仅报告审计前已有的 `ChatMessageList.tsx` 尾随空格，
  本次未触碰该文件。
- 最后一次全量测试后已清理 Ftre/Core 的 `__pycache__`、`.pytest_cache`、`.ruff_cache`、
  `build`、`dist` 和临时 F36 构建目录；复核剩余数量为 0。Core `work/` 与
  `src/tests/data/logs/` 用户数据保留。
- 本次审计未创建提交；工作区仍保留各仓库既有未提交变更，Desktop 的
  `packages/renderer/src/app/Workbench.tsx` 为审计前已有改动且未触碰。

## 5. 交付门禁

- PRD：`docs/prd/PRD-F36-agent-core-consolidation.md` 已将 FR1–FR15、AC1–AC15 勾选，
  状态为“已验收”。
- TODO：F36.1–F36.8 均为 `done`；Core C8 为 `done`。
- CHANGELOG：F36 与 Core C8 退休记录已写入。
- Gateway：本阶段未 kill、restart、迁移或触碰运行中的任务。
- Git：Ftre、Desktop、Core 仍保持各自 feature/工作分支，未在本地合并 develop；后续
  按各仓库 PR 门禁分别提交和评审。
